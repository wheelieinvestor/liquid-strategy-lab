"""One fenced executor and durable once-only decisions for all approved sleeves."""

import hashlib
import json
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from liquid_autonomous_trader.flow_position_runner import FlowPositionRunner
from liquid_autonomous_trader.liquid_operations import LiquidOperationBlocked
from liquid_autonomous_trader.live_policy import Strategy
from liquid_autonomous_trader.local_accounting import AccountingError
from liquid_autonomous_trader.runtime_diagnostics import (
    admission_diagnostic,
    exception_diagnostic,
    expected_xyz_wait,
)


class MultiAgentRuntime:
    def __init__(self, btc, flow, gamma, funding, *, clock, cramer=None):
        self.btc, self.flow, self.gamma, self.funding, self.clock = btc, flow, gamma, funding, clock
        self.cramer = cramer
        self.join = btc.join
        self.lifecycle = self.join.lifecycle
        if not self.lifecycle.shared_account:
            raise ValueError("shared_account_scope_required")
        self.db = self.lifecycle.risk_store.db
        self.runner = FlowPositionRunner(self.lifecycle, funding, clock=clock)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sleeve_runtime_decisions("
            "decision_id TEXT PRIMARY KEY,strategy TEXT NOT NULL,intent_id TEXT UNIQUE NOT NULL,"
            "plan TEXT NOT NULL,state TEXT NOT NULL,diagnostic TEXT,observed_at TEXT NOT NULL)"
        )

    def recover_closed_positions(self):
        from liquid_autonomous_trader.closed_position_recovery import recover_closed_flow

        return recover_closed_flow(self.lifecycle, self.funding)

    def managed_activation_ready(self):
        owners = self.lifecycle.journal.db.execute(
            "SELECT owner_intent FROM liquid_market_owners"
        ).fetchall()
        if not owners:
            return False
        if any(
            o["state"] != "acknowledged" or o["kind"] != "entry"
            for o in self.lifecycle.journal.unresolved()
        ):
            return False
        for row in owners:
            record, _, _, evidence = self.lifecycle._owned(row[0])
            projection = self.lifecycle.reconciler.accounting_projection
            if (
                record["state"] != "open"
                or not evidence.protection_verified
                or projection is None
                or projection.blockers
            ):
                return False
            if (
                record["strategy"] == "flow_show_mirror"
                and not self.db.execute(
                    "SELECT 1 FROM flow_runtime_positions WHERE owner=?", (row[0],)
                ).fetchone()
            ):
                return False
        return True

    def tick(self, *, allow_entries):
        self.lifecycle._fence()
        if getattr(self, "jev", None) is not None:
            self.jev.retire_closed_reviews()
        results = {}
        # Reconcile/protect every owner first. One sleeve's stale source cannot
        # suppress another sleeve's exits; the common journal still blocks replay.
        owners = self.lifecycle.journal.db.execute(
            "SELECT owner_intent,symbol FROM liquid_market_owners"
        ).fetchall()
        for owner in owners:
            intent = owner["owner_intent"]
            record = self.lifecycle.risk_store.execution(intent)
            try:
                if record["strategy"] == Strategy.BTC.value:
                    value = self.btc._manage(intent)
                elif record["strategy"] == Strategy.FLOW.value:
                    if (
                        self.db.execute(
                            "SELECT 1 FROM flow_runtime_positions WHERE owner=?", (intent,)
                        ).fetchone()
                        is None
                    ):
                        plan = self._plan(intent)
                        # Recover registration after an acknowledged entry, without
                        # changing the pre-entry funding baseline or sending entry again.
                        self.runner.register(
                            intent,
                            delivery_id=plan["delivery_id"],
                            quantity_step=Decimal(plan["quantity_step"]),
                            price_step=Decimal(plan["price_step"]),
                        )
                    value = self.runner.tick(intent)
                elif record["strategy"] == Strategy.XYZ.value:
                    value = self._gamma_manage(intent)
                elif record["strategy"] == Strategy.CRAMER.value and self.cramer is not None:
                    value = self.cramer.manage(intent, self._plan(intent))
                else:
                    raise ValueError("unknown_strategy_management_blocked")
                results[intent] = {"result": value}
                if intent in self.runner.funding_unavailable:
                    results[intent].update(funding_telemetry="unavailable")
            except Exception as exc:
                results[intent] = exception_diagnostic(exc)
        if not allow_entries or any(
            "error_type" in r or r.get("result") == "unknown_write_reconciliation_only"
            for r in results.values()
        ):
            return {"owners": results, "entries_enabled": False}
        # Each source failure is isolated, while the account writer remains shared.
        entries = {}
        for name, source in [("flow_show_mirror", self.flow), ("xyz100_gex", self.gamma)]:
            try:
                bundle = source()
                entries[name] = (
                    {"result": "waiting_for_signal"} if bundle is None else self._entry(bundle)
                )
                if bundle is not None and name == "flow_show_mirror":
                    self.flow.acknowledge(bundle["plan"]["source_sequence"])
            except Exception as exc:
                waiting = expected_xyz_wait(exc, self.clock()) if name == "xyz100_gex" else None
                entries[name] = waiting or exception_diagnostic(exc)
        try:
            btc_result = self.btc.tick()
            entries["btc_momentum"] = {
                "result": btc_result if isinstance(btc_result, str) else "entry_observed"
            }
        except Exception as exc:
            entries["btc_momentum"] = exception_diagnostic(exc)
        if self.cramer is not None:
            try:
                entries["inverse_cramer"] = self.cramer.next_entry(self)
            except Exception as exc:
                entries["inverse_cramer"] = exception_diagnostic(exc)
        return {"owners": results, "entries": entries, "entries_enabled": True}

    def _entry(self, bundle):
        strategy = bundle["strategy"]
        key = bundle["decision_id"]
        if (
            strategy not in {Strategy.FLOW, Strategy.XYZ, Strategy.CRAMER}
            or not isinstance(key, str)
            or not 0 < len(key) <= 160
        ):
            raise ValueError("sleeve_decision_identity_invalid")
        existing = self.db.execute(
            "SELECT state FROM sleeve_runtime_decisions WHERE decision_id=?", (key,)
        ).fetchone()
        if existing:
            return {"result": "decision_already_consumed", "state": existing[0]}
        intent = strategy.value + "-" + hashlib.sha256(key.encode()).hexdigest()[:32]
        request = bundle["request"]
        plan = bundle["plan"]
        self.db.execute(
            "INSERT INTO sleeve_runtime_decisions VALUES(?,?,?,?,?,?,?)",
            (
                key,
                strategy.value,
                intent,
                json.dumps(plan, sort_keys=True),
                "dispatching" if request else "hold",
                json.dumps({"reasons": bundle["reasons"]}, sort_keys=True),
                self.clock().isoformat(),
            ),
        )
        if request is None:
            return {"result": "hold", "reasons": bundle["reasons"]}
        try:
            if strategy is Strategy.FLOW:
                try:
                    # Local baseline only: funding telemetry cannot age the entry quote.
                    self.funding.begin(
                        intent,
                        Decimal(plan["funding_reserve"]),
                        symbol=request.symbol,
                        side=request.side,
                        observe=False,
                    )
                except AccountingError:
                    self.runner.funding_unavailable.add(intent)
            self.join.enter(
                intent_id=intent,
                request_hash=hashlib.sha256(repr(request).encode()).hexdigest(),
                request=request,
                reconciliation=bundle["reconciliation"],
                decision_id=key,
            )
            self.db.execute(
                "UPDATE sleeve_runtime_decisions SET state='submitted',observed_at=? WHERE "
                "decision_id=?",
                (self.clock().isoformat(), key),
            )
            if strategy is Strategy.FLOW:
                try:
                    self.runner.register(
                        intent,
                        delivery_id=plan["delivery_id"],
                        quantity_step=Decimal(plan["quantity_step"]),
                        price_step=Decimal(plan["price_step"]),
                    )
                except Exception:
                    # The accepted position cannot be left without its exit plan.
                    self.lifecycle.close_owned(
                        intent, request_id=intent + ":runner-registration-recovery"
                    )
                    raise
            if strategy is Strategy.CRAMER:
                if self.cramer is None:
                    raise ValueError("cramer_manager_missing")
                self.cramer.register_position(intent, plan)
            return {"result": "entry_observed", "intent_id": intent}
        except Exception as exc:
            record = self.lifecycle.risk_store.execution(intent)
            operation = self.lifecycle.journal.operation(intent)
            before_dispatch = operation is None and (
                record is None or record["state"] in {"reserved", "released", "rejected"}
            )
            if before_dispatch and record and record["state"] == "reserved":
                self.lifecycle.risk_store.transition(
                    intent,
                    ("reserved",),
                    "released",
                    writer_id=self.lifecycle.writer_id,
                    fence_token=self.lifecycle.fence_token,
                    now=self.clock(),
                    detail={"reason": "sleeve_admission_failed_before_dispatch"},
                )
            diagnostic = {
                **exception_diagnostic(exc),
                "inputs": admission_diagnostic(request),
                "stage": "before_dispatch" if before_dispatch else "execution_or_reconciliation",
                "operation_state": operation["state"] if operation else None,
            }
            self.db.execute(
                "UPDATE sleeve_runtime_decisions SET state=?,diagnostic=?,observed_at=? "
                "WHERE decision_id=?",
                (
                    "rejected" if before_dispatch else "unresolved",
                    json.dumps(diagnostic, sort_keys=True),
                    self.clock().isoformat(),
                    key,
                ),
            )
            return {"result": "rejected" if before_dispatch else "unresolved", **diagnostic}

    def _plan(self, intent):
        row = self.db.execute(
            "SELECT plan FROM sleeve_runtime_decisions WHERE intent_id=?", (intent,)
        ).fetchone()
        if row is None:
            raise LiquidOperationBlocked("sleeve_exit_plan_missing")
        return json.loads(row[0])

    def _gamma_manage(self, intent):
        evidence = self.lifecycle.reconcile(intent)
        if any(
            o["state"] != "acknowledged" or o["kind"] != "entry"
            for o in self.lifecycle.journal.unresolved()
        ):
            return "unknown_write_reconciliation_only"
        p = evidence.position
        if p is None:
            return "flat_reconciled"
        if not evidence.protection_verified:
            self.lifecycle.close_owned(intent, request_id=intent + ":protection-recovery")
            return "unprotected_position_reduced"
        plan = self._plan(intent)
        if getattr(self, "jev", None) is not None:
            gex = getattr(getattr(self.gamma, "source", None), "latest_exit_context", None)
            if (
                gex
                and not 0
                <= (self.clock() - datetime.fromisoformat(gex["observed_at"])).total_seconds()
                <= 300
            ):
                gex = None
            result = self.jev.manage(intent, plan, evidence, context={"current_gex": gex})
            if result is not None:
                return result
            if getattr(self.jev, "stop_focused", False):
                # September 22: preserve quantity and native protection on uncertainty.
                return "jev_stop_preserved"
        target = Decimal(plan["target"])
        if (p.side == "long" and p.mark_price >= target) or (
            p.side == "short" and p.mark_price <= target
        ):
            self.lifecycle.close_owned(intent, request_id=intent + ":target-close")
            return "target_close"
        if getattr(self, "jev", None) is not None:
            return "jev_stop_preserved"
        record = self.lifecycle.risk_store.execution(intent)
        risk = abs(p.entry_price - Decimal(record["initial_stop"]))
        profit = (p.mark_price - p.entry_price) * (1 if p.side == "long" else -1)
        if profit < risk:
            return "protected_hold"
        atr = self.gamma.management_atr()
        if atr <= 0:
            raise ValueError("gamma_exit_atr_invalid")
        candidate = p.mark_price + atr * (-1 if p.side == "long" else 1)
        step = Decimal(plan["price_step"])
        candidate = (candidate / step).to_integral_value(
            rounding=ROUND_FLOOR if p.side == "long" else ROUND_CEILING
        ) * step
        if (
            candidate <= 0
            or (p.side == "long" and candidate <= p.stop_price)
            or (p.side == "short" and candidate >= p.stop_price)
        ):
            return "protected_hold"
        request = intent + ":trail:" + hashlib.sha256(str(candidate).encode()).hexdigest()[:16]
        self.lifecycle.tighten_stop(intent, request_id=request, stop=candidate)
        return "stop_tightened"
