"""Serialized BTC scheduling around the guarded Liquid join.

Account/venue risk must come from an installed trusted provider, not shadow JSON.
The scheduler never supplies fee, liquidation, precision, or P&L defaults. Its
durable decisions share the execution database so a restart cannot resubmit an
already considered bar. This module does not enable the broker automation policy.
"""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from liquid_autonomous_trader.btc_mandate import BTC_MAX_ORDER_NOTIONAL_USD
from liquid_autonomous_trader.frozen.models import SignalAction
from liquid_autonomous_trader.frozen.strategies.btc_momentum import (
    BtcMomentumConfigV1,
    BtcMomentumEngineV1,
    BtcMomentumObservationV1,
)
from liquid_autonomous_trader.liquid_operations import LiquidOperationBlocked
from liquid_autonomous_trader.live_policy import Strategy
from liquid_autonomous_trader.runtime_diagnostics import admission_diagnostic, exception_diagnostic


class BtcExecutionRuntime:
    def __init__(self, join, *, source, risk_provider, clock=lambda: datetime.now(UTC)):
        self.join, self.source, self.risk_provider, self.clock = join, source, risk_provider, clock
        self.db = join.controller.execution_store.db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS btc_runtime_decisions("
            "decision_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL,"
            "plan TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS btc_entry_diagnostics("
            "decision_id TEXT PRIMARY KEY, body TEXT NOT NULL, observed_at TEXT NOT NULL)"
        )

    def run(self, *, stop, report, interval=5):
        """Renew the existing writer lease; retry reads, never replay dispatches.

        Run on the owning thread of the SQLite stores. A lost writer lease ends
        this loop rather than acquiring a new fence over unresolved work.
        """
        if not 5 <= interval <= 30:
            raise ValueError("btc_runtime_interval_invalid")
        while not stop.is_set():
            lifecycle = self.join.lifecycle
            lifecycle.risk_store.renew_writer(
                lifecycle.writer_id, lifecycle.fence_token, self.clock(), 120
            )
            try:
                result = self.tick()
                report(
                    {
                        "cycle": "completed",
                        "result": result if isinstance(result, str) else "entry_observed",
                    }
                )
            except Exception as exc:
                # Do not print provider exception messages or raw account payloads.
                report({"cycle": "blocked", **exception_diagnostic(exc)})
            stop.wait(interval)

    def tick(self):
        """Management takes priority over any new source or risk-admission work."""
        with self.join.controller._lock:
            lifecycle = self.join.lifecycle
            lifecycle._fence()
            owners = lifecycle.journal.db.execute(
                "SELECT owner_intent FROM liquid_market_owners"
                + (" WHERE symbol='BTC'" if lifecycle.shared_account else "")
            ).fetchall()
            if len(owners) > 1:
                raise LiquidOperationBlocked("btc_runtime_multiple_market_owners")
            if owners:
                return self._manage(owners[0]["owner_intent"])
            if any(
                not lifecycle.shared_account or o["state"] != "acknowledged" or o["kind"] != "entry"
                for o in lifecycle.journal.unresolved()
            ):
                raise LiquidOperationBlocked("prior_write_outcome_unresolved")
            decision_id, observation = self.source()
            observation = BtcMomentumObservationV1.model_validate(observation)
            self._fresh(observation)
            # Account reconciliation follows the public fetch so quote acquisition
            # does not silently age the account inputs used for admission.
            account, orders, evidence = lifecycle.reconciler.position(
                all_positions=lifecycle.shared_account
            )
            if not lifecycle.shared_account and (
                account.positions or orders.orders or account.margin_used_usd
            ):
                raise LiquidOperationBlocked("unattributed_account_exposure_at_bootstrap")
            if lifecycle.reconciler.accounting_error:
                raise LiquidOperationBlocked("local_accounting_unavailable")
            if not decision_id or len(decision_id) > 128:
                raise ValueError("btc_runtime_decision_identity_invalid")
            if self.db.execute(
                "SELECT 1 FROM btc_runtime_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone():
                return "decision_already_consumed"
            signal = BtcMomentumEngineV1(
                BtcMomentumConfigV1(funding_filter_enabled=False)
            ).evaluate(observation)
            if signal.action != SignalAction.ENTER:
                self._record(decision_id, {}, "hold")
                return "hold"
            if self.risk_provider is None:
                raise LiquidOperationBlocked("production_account_and_venue_risk_provider_missing")
            request, reconciled, tick_size = self.risk_provider(
                observation, signal, account, orders, evidence
            )
            projection = lifecycle.reconciler.accounting_projection
            if projection is not None:
                reconciled = replace(reconciled, risk=projection.apply(reconciled.risk))
            if (
                request.strategy != Strategy.BTC
                or request.symbol != "BTC"
                or request.side != signal.direction.value
                or request.entry != observation.market.price
                or request.proposed_stop != signal.bracket.stop_price
                or request.requested_notional > BTC_MAX_ORDER_NOTIONAL_USD
                or request.selected_leverage > signal.leverage
                or not isinstance(tick_size, Decimal)
                or not tick_size.is_finite()
                or tick_size <= 0
            ):
                raise LiquidOperationBlocked("btc_runtime_risk_request_mismatch")
            self._fresh(observation)  # Risk/account requests may have taken time.
            plan = {
                "target": str(signal.bracket.target_price),
                "tick_size": str(tick_size),
                "entry_observation": observation.model_dump(mode="json"),
            }
            intent = self._record(decision_id, plan, "dispatching")
            # Persist the consumed decision BEFORE admission/POST. On failure it is
            # not automatically retried. Any created reservation remains durable.
            try:
                result = self.join.enter(
                    intent_id=intent,
                    request_hash=hashlib.sha256(repr(request).encode()).hexdigest(),
                    request=request,
                    reconciliation=reconciled,
                    decision_id=decision_id,
                )
            except Exception as exc:
                record = lifecycle.risk_store.execution(intent)
                operation = lifecycle.journal.operation(intent)
                if record and record["state"] == "reserved" and not operation:
                    # There is no dispatched broker obligation for this reservation.
                    lifecycle.risk_store.transition(
                        intent,
                        ("reserved",),
                        "released",
                        writer_id=lifecycle.writer_id,
                        fence_token=lifecycle.fence_token,
                        now=self.clock(),
                        detail={"reason": "runtime_admission_failed_before_dispatch"},
                    )
                # Only a proven pre-dispatch failure can be labeled rejected.
                # Unknown/dispatched writes retain their journal and decision state.
                if operation is None and (
                    record is None or record["state"] in {"reserved", "released"}
                ):
                    body = {**exception_diagnostic(exc), "inputs": admission_diagnostic(request)}
                    self.db.execute(
                        "INSERT INTO btc_entry_diagnostics VALUES(?,?,?)",
                        (decision_id, json.dumps(body, sort_keys=True), self.clock().isoformat()),
                    )
                    self.db.execute(
                        "UPDATE btc_runtime_decisions SET state='rejected',updated_at=? "
                        "WHERE intent_id=?",
                        (self.clock().isoformat(), intent),
                    )
                raise
            self.db.execute(
                "UPDATE btc_runtime_decisions SET state='submitted',updated_at=? WHERE intent_id=?",
                (self.clock().isoformat(), intent),
            )
            return result

    def _record(self, decision_id, plan, state):
        intent = "btc-" + hashlib.sha256(decision_id.encode()).hexdigest()[:40]
        self.db.execute(
            "INSERT INTO btc_runtime_decisions VALUES(?,?,?,?,?)",
            (
                decision_id,
                intent,
                json.dumps(plan, sort_keys=True),
                state,
                self.clock().isoformat(),
            ),
        )
        return intent

    def _fresh(self, observation):
        age = (self.clock() - observation.market.observed_at).total_seconds()
        if not 0 <= age <= 5:
            raise LiquidOperationBlocked("btc_runtime_market_data_stale")

    def _manage(self, intent):
        lifecycle = self.join.lifecycle
        evidence = lifecycle.reconcile(intent)
        # Unknown closes/stops can affect future positions. Read and preserve the
        # obligation, without issuing another management request or accepting entry.
        if any(
            o["state"] != "acknowledged" or o["kind"] != "entry"
            for o in lifecycle.journal.unresolved()
        ):
            return "unknown_write_reconciliation_only"
        p = evidence.position
        if p is None:
            return "flat_reconciled"
        if not evidence.protection_verified:
            lifecycle.close_owned(intent, request_id=intent + ":protection-recovery")
            return "unprotected_position_reduced"
        row = self.db.execute(
            "SELECT plan FROM btc_runtime_decisions WHERE intent_id=?", (intent,)
        ).fetchone()
        if row is None:
            raise LiquidOperationBlocked("btc_runtime_exit_plan_missing")
        plan = json.loads(row["plan"])
        if getattr(self, "jev", None) is not None:
            result = self.jev.manage(intent, plan, evidence)
            if result is not None:
                return result
            if getattr(self.jev, "stop_focused", False):
                # September 22: model failure cannot revive a legacy target exit.
                return "jev_stop_preserved"
        target = Decimal(plan["target"])
        if (p.side == "long" and p.mark_price >= target) or (
            p.side == "short" and p.mark_price <= target
        ):
            lifecycle.close_owned(intent, request_id=intent + ":target-close")
            return "target_close"
        if getattr(self, "jev", None) is not None:
            return "jev_stop_preserved"
        # Current ATR is obtained from the validated source, not generated text.
        _, observation = self.source()
        observation = BtcMomentumObservationV1.model_validate(observation)
        self._fresh(observation)
        record = lifecycle.risk_store.execution(intent)
        initial_risk = abs(p.entry_price - Decimal(record["initial_stop"]))
        profit_distance = (p.mark_price - p.entry_price) * (1 if p.side == "long" else -1)
        if profit_distance < initial_risk:
            return "protected_hold"
        step = Decimal(plan["tick_size"])
        candidate = p.mark_price + observation.market.atr_15m * (-1 if p.side == "long" else 1)
        candidate = (candidate / step).to_integral_value(
            rounding=ROUND_FLOOR if p.side == "long" else ROUND_CEILING
        ) * step
        if (
            candidate <= 0
            or (p.side == "long" and candidate <= p.stop_price)
            or (p.side == "short" and candidate >= p.stop_price)
        ):
            return "protected_hold"
        request_id = intent + ":trail:" + hashlib.sha256(str(candidate).encode()).hexdigest()[:16]
        lifecycle.tighten_stop(intent, request_id=request_id, stop=candidate)
        return "stop_tightened"
