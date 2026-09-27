"""One causal portfolio driver: production decisions, simulated writes and ledger.

Source observations are processed before management, then entries in the running
executor's Flow/XYZ/BTC/Cramer order. New commands require a later executable
observation. Missing evidence is retained, never replaced by synthetic history.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from itertools import groupby
from types import SimpleNamespace as NS

from liquid_autonomous_trader.backtesting.admission import btc_request, utc
from liquid_autonomous_trader.backtesting.btc import BtcAdapter
from liquid_autonomous_trader.backtesting.events import Event, canonical, digest
from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
from liquid_autonomous_trader.backtesting.jev import CachedJev, ModelRequest
from liquid_autonomous_trader.backtesting.ledger import serial
from liquid_autonomous_trader.backtesting.sources import SourceAdapters
from liquid_autonomous_trader.btc_execution_runtime import BtcExecutionRuntime
from liquid_autonomous_trader.flow_position_runner import FlowPositionRunner
from liquid_autonomous_trader.frozen.models import StrategySignalV1
from liquid_autonomous_trader.frozen.strategies.btc_momentum import BtcMomentumObservationV1
from liquid_autonomous_trader.jev_exit_manager import position_state
from liquid_autonomous_trader.local_accounting import AccountingError
from liquid_autonomous_trader.multi_agent_runtime import MultiAgentRuntime

POLICIES = {
    "current-v5-exact",
    "legacy-production",
    "breakeven-1R",
    "breakeven-1.25R",
    "breakeven-1.5R",
}


class ReplayJev:
    stop_focused = True

    def __init__(self, engine, cache):
        self.engine, self.cached = engine, CachedJev(cache) if cache is not None else None

    def manage(self, owner, plan, evidence, *, context=None):
        engine, p = self.engine, evidence.position
        if p is None or not evidence.protection_verified:
            return None
        rows = engine.inputs.rows("jev_request", p.broker_symbol)
        reason, result = "exact_cache_missing", None
        if rows and self.cached is not None:
            request = ModelRequest(canonical(rows[-1].payload))
            state = request.state
            order = engine.sim.orders[owner]
            original = state["original"]
            # Request features are original recorded evidence. Any changed plan,
            # entry cohort or path must miss instead of borrowing the old answer.
            if (
                state["plan"] != plan
                or D(original["original_stop"]) != order.stop
                or original["opened_at"] != utc(order.submitted_us).isoformat()
            ):
                reason = "recorded_entry_or_plan_mismatch"
            else:
                try:
                    market = engine.inputs.latest(
                        "exit_market", p.broker_symbol, maximum_age_us=5_000_000
                    ).payload
                    result = self.cached.decide(
                        request,
                        engine.inputs.now_us,
                        position=position_state(p),
                        quote=market,
                        context=context or {},
                    )
                    reason = result["reason"]
                    for index, command in enumerate(result["commands"]):
                        engine.account.commands.append(
                            {
                                **command,
                                "owner": owner,
                                "allow_loosen": True,
                                "request_id": owner + ":jev:" + request.key + ":" + str(index),
                            }
                        )
                except ValueError as error:
                    reason = safe_reason(error)
        engine.reviews.append(
            {"owner": owner, "at_us": engine.inputs.now_us, "reason": reason, "result": result}
        )
        return (
            result.get("production_outcome") or "jev_stop_preserved"
            if result
            else "jev_stop_preserved"
        )


def safe_reason(error):
    # Validation libraries may echo source text. Only symbolic messages escape.
    value = str(error)
    return (
        value
        if value and all(c.isalnum() or c in "_:." for c in value) and len(value) < 160
        else type(error).__name__
    )


class PortfolioEngine:
    def __init__(
        self,
        simulation,
        *,
        policy="current-v5-exact",
        model_cache=None,
        enabled=("flow_show_mirror", "xyz100_gex", "btc_momentum", "inverse_cramer"),
        margin_mode="cross",
        latency_us=1_000_000,
        mode="recorded_decision_replay",
    ):
        if policy not in POLICIES or margin_mode not in {"cross", "isolated"}:
            raise ValueError("unknown_research_policy_or_margin_mode")
        if mode not in {
            "recorded_decision_replay",
            "historical_execution_simulation",
            "synthetic_stress",
            "forward_observation_replay",
        }:
            raise ValueError("unknown_replay_mode")
        self.sim, self.policy = simulation, policy
        self.enabled, self.margin_mode, self.latency_us, self.mode = (
            tuple(enabled),
            margin_mode,
            latency_us,
            mode,
        )
        self.sources = SourceAdapters(simulation)
        self.inputs, self.account = self.sources.inputs, self.sources.account
        self.btc = BtcAdapter()
        self.plans, self.consumed = {}, set()
        self.decisions, self.reviews, self.curve = [], [], []
        self.last_btc = None
        self.applied_events = set()
        self.termination_reason = None
        self.jev = ReplayJev(self, model_cache)
        # Construct only the management objects' data dependencies. Their live
        # constructors, controllers, writers and transport factories are absent.
        self.btc_manager = object.__new__(BtcExecutionRuntime)
        self.btc_manager.join = NS(lifecycle=self.account)
        self.btc_manager.db, self.btc_manager.clock = self.account.db, self.inputs.clock
        self.btc_manager.source = self._btc_management_observation
        self.gamma_manager = object.__new__(MultiAgentRuntime)
        self.gamma_manager.lifecycle, self.gamma_manager.gamma = self.account, self.sources.gamma
        self.gamma_manager.clock = self.inputs.clock
        self.gamma_manager._plan = lambda key: self.plans[key]

        def unavailable(*args, **kwargs):
            raise AccountingError("research_native_funding_usage_unavailable")

        funding = NS(cached_usage=lambda _: None, bind=lambda *a, **k: None, observe=unavailable)
        self.flow_runner = FlowPositionRunner(self.account, funding, clock=self.inputs.clock)
        if policy == "current-v5-exact":
            for manager in (
                self.btc_manager,
                self.gamma_manager,
                self.flow_runner,
                self.sources.cramer,
            ):
                manager.jev = self.jev
        elif policy.startswith("breakeven-"):
            # Candidate owns discretion, while production mandatory protection and
            # Cramer's fixed maximum-hold deadline continue to run first.
            candidate = NS(stop_focused=True, manage=self._candidate_manage)
            for manager in (
                self.btc_manager,
                self.gamma_manager,
                self.flow_runner,
                self.sources.cramer,
            ):
                manager.jev = candidate

    def _btc_management_observation(self):
        decision = self.btc.decide(self.inputs.events, self.inputs.now_us)
        if decision.signal is None:
            raise ValueError("btc_management_market_unavailable")
        # Build the exact observation through the same causal production adapter.
        return "research", decision.observation

    def _candidate_manage(self, owner, plan, evidence, *, context=None):
        p, order = evidence.position, self.sim.orders[owner]
        risk = abs(p.entry_price - order.stop)
        direction = 1 if p.side == "long" else -1
        threshold = D(self.policy.removeprefix("breakeven-").removesuffix("R"))
        if risk and direction * (p.mark_price - p.entry_price) >= threshold * risk:
            step = self.sim.ledger.instruments[p.broker_symbol].price_step
            stop = (p.entry_price / step).to_integral_value(
                rounding=ROUND_CEILING if direction > 0 else ROUND_FLOOR
            ) * step
            if direction * (stop - p.stop_price) > 0:
                self.account.tighten_stop(owner, request_id=owner + ":" + self.policy, stop=stop)
        return "jev_candidate_stop_preserved"

    def _entry(self, bundle):
        now_us = self.inputs.now_us
        key = bundle["decision_id"]
        if key in self.consumed:
            return {"result": "decision_already_consumed"}
        self.consumed.add(key)
        request, plan = bundle["request"], bundle["plan"]
        reason = "hold"
        if request is not None:
            self.plans[key] = plan
            # Software-managed targets are evaluated by production management;
            # they are not silently promoted into native resting take-profits.
            reason = self.sim.submit(
                key,
                request,
                now_us,
                mode=self.margin_mode,
                expires_us=now_us + 60_000_000,
                latency_us=self.latency_us,
            )
            if key in self.sim.orders:
                table = (
                    "btc_runtime_decisions"
                    if request.strategy.value == "btc_momentum"
                    else "sleeve_runtime_decisions"
                )
                self.account.db.execute(
                    "INSERT OR REPLACE INTO " + table + " VALUES(?,?)", (key, canonical(plan))
                )
                self.account.sync()
        self.decisions.append(
            {
                "at_us": now_us,
                "strategy": bundle["strategy"].value,
                "decision_id": key,
                "reason": reason,
                "source_reasons": list(bundle["reasons"]),
                "plan": plan,
                "request": serial(asdict(request)) if request else None,
            }
        )
        return {"result": reason}

    def _plan(self, owner):
        return self.plans[owner]

    def _btc_entry(self):
        decision = self.btc.decide(self.inputs.events, self.inputs.now_us)
        self.last_btc = asdict(decision)
        if decision.signal is None:
            raise ValueError("btc_" + (decision.reasons[0] if decision.reasons else "unsupported"))
        observation = BtcMomentumObservationV1.model_validate(decision.observation)
        signal = StrategySignalV1.model_validate(decision.signal)
        key = "btc-v1:" + str(decision.features["bar_end_ms"])
        request = (
            btc_request(self.sim.ledger, observation, signal, self.inputs.now_us)
            if signal.action.value == "enter"
            else None
        )
        plan = (
            {
                "target": str(signal.bracket.target_price),
                "tick_size": str(self.sim.ledger.instruments["BTC"].price_step),
                "entry_observation": decision.observation,
            }
            if request
            else {}
        )
        from liquid_autonomous_trader.live_policy import Strategy

        return self._entry(
            {
                "decision_id": key,
                "strategy": Strategy.BTC,
                "request": request,
                "plan": plan,
                "reasons": decision.reasons,
            }
        )

    def _flush_management(self):
        commands, self.account.commands = self.account.commands, []
        for command in commands:
            key, owner = command["request_id"], command["owner"]
            if key in self.sim.management:
                continue  # the existing journal obligation owns retry/recovery
            try:
                result = self.sim.manage(
                    key,
                    owner,
                    command["action"],
                    self.inputs.now_us,
                    stop=command.get("stop"),
                    quantity=command.get("quantity"),
                    allow_loosen=command.get("allow_loosen", False),
                    latency_us=self.latency_us,
                )
            except ValueError as error:
                result = safe_reason(error)
            self.decisions.append(
                {
                    "at_us": self.inputs.now_us,
                    "owner": owner,
                    "management": command["action"],
                    "reason": result,
                }
            )
        self.account.sync()

    def tick(self):
        self.account.sync()
        failures = False
        owners = self.account.db.execute("SELECT owner_intent FROM liquid_market_owners").fetchall()
        for row in owners:
            owner = row[0]
            record = self.account.execution(owner)
            if record["state"] != "open":
                continue
            strategy, plan = record["strategy"], self.plans[owner]
            try:
                if strategy == "btc_momentum":
                    result = self.btc_manager._manage(owner)
                elif strategy == "xyz100_gex":
                    result = self.gamma_manager._gamma_manage(owner)
                elif strategy == "flow_show_mirror":
                    if not self.account.db.execute(
                        "SELECT 1 FROM flow_runtime_positions WHERE owner=?", (owner,)
                    ).fetchone():
                        self.flow_runner.register(
                            owner,
                            delivery_id=plan["delivery_id"],
                            quantity_step=D(plan["quantity_step"]),
                            price_step=D(plan["price_step"]),
                        )
                    result = self.flow_runner.tick(owner)
                else:
                    result = self.sources.cramer.manage(owner, plan)
                failures |= result == "unknown_write_reconciliation_only"
            except (ValueError, RuntimeError, KeyError, ArithmeticError) as error:
                failures, result = True, safe_reason(error)
            self.decisions.append(
                {
                    "at_us": self.inputs.now_us,
                    "owner": owner,
                    "strategy": strategy,
                    "management": "review",
                    "reason": result,
                }
            )
        self._flush_management()
        if failures or self.sim.halted:
            return
        for name, source in (
            ("flow_show_mirror", self.sources.flow),
            ("xyz100_gex", self.sources.gamma),
            ("btc_momentum", None),
            ("inverse_cramer", None),
        ):
            if name not in self.enabled:
                continue
            try:
                if name == "btc_momentum":
                    self._btc_entry()
                elif name == "inverse_cramer":
                    result = self.sources.cramer.next_entry(self)
                    self.decisions.append(
                        {"at_us": self.inputs.now_us, "strategy": name, "reason": result["result"]}
                    )
                else:
                    bundle = source()
                    if bundle is not None:
                        self._entry(bundle)
                        if name == "flow_show_mirror":
                            self.sources.flow.acknowledge(bundle["plan"]["source_sequence"])
            except (ValueError, RuntimeError, KeyError, ArithmeticError) as error:
                self.decisions.append(
                    {
                        "at_us": self.inputs.now_us,
                        "strategy": name,
                        "reason": safe_reason(error),
                        "status": "unsupported",
                    }
                )
        self._flush_management()

    def run(self, events, *, end_us=None):
        if self.termination_reason:
            raise ValueError("terminated_replay_cannot_continue")
        ordered = sorted(events, key=lambda e: e.order)
        for now_us, group in groupby(ordered, key=lambda e: e.available_us):
            if end_us is not None and now_us > end_us:
                break
            group = list(group)
            self.inputs.advance(now_us, group)
            for event in group:
                if event.identity in self.applied_events:
                    continue
                self.applied_events.add(event.identity)
                p = event.payload
                if (
                    event.kind in {"native_book", "book"}
                    and event.instrument in self.sim.ledger.instruments
                ):
                    observed_us = p["time"] * 1000
                    if (
                        not observed_us <= event.event_us <= now_us
                        or now_us - observed_us > 5_000_000
                    ):
                        self.decisions.append(
                            {
                                "at_us": now_us,
                                "status": "unsupported",
                                "reason": "noncausal_or_stale_execution_book",
                            }
                        )
                        continue
                    self.sim.quote(
                        event.instrument,
                        now_us,
                        bids=[(v["px"], v["sz"]) for v in p["levels"][0]],
                        asks=[(v["px"], v["sz"]) for v in p["levels"][1]],
                    )
                elif event.kind == "funding_settlement":
                    if now_us != event.event_us:
                        self.decisions.append(
                            {
                                "at_us": now_us,
                                "status": "unsupported",
                                "reason": "late_funding_requires_asof_position_join",
                            }
                        )
                        continue
                    self.sim.ledger.apply(
                        "funding:" + event.identity,
                        now_us,
                        "funding",
                        symbol=event.instrument,
                        rate=p["rate"],
                        oracle_price=p["oracle"],
                        settlement_us=event.event_us,
                        native_timestamp=True,
                    )
            if self.sim.ledger.breaches():
                self.termination_reason = (
                    "unsupported_liquidation_execution_after_maintenance_breach"
                )
            else:
                self.tick()
            self.sim.ledger.reconcile()
            self.curve.append({"at_us": now_us, **self.sim.ledger.state()})
            if self.termination_reason:
                break
        return self.result()

    def result(self):
        core = {
            "schema": "liquid-portfolio-replay-v1",
            "mode": self.mode,
            "policy": self.policy,
            "enabled": list(self.enabled),
            "margin_mode": self.margin_mode,
            "latency_us": self.latency_us,
            "simulation": self.sim.state(),
            "ledger_journal": self.sim.ledger.journal,
            "decisions": self.decisions,
            "jev_reviews": self.reviews,
            "equity_curve": self.curve,
            "source_state": self.sources.source_state(),
            "data_hash": digest(sorted(self.inputs.identities.items())),
            "termination_reason": self.termination_reason,
        }
        return json.loads(canonical({**core, "core_sha256": digest(core)}))

    def close(self):
        if self.jev.cached:
            self.jev.cached.close()
        self.sources.close()

    def checkpoint(self):
        cached = self.jev.cached
        payload = {
            "schema": "liquid-portfolio-checkpoint-v1",
            "simulation": self.sim.checkpoint(),
            "policy": self.policy,
            "enabled": list(self.enabled),
            "mode": self.mode,
            "margin_mode": self.margin_mode,
            "latency_us": self.latency_us,
            "plans": self.plans,
            "consumed": sorted(self.consumed),
            "decisions": self.decisions,
            "reviews": self.reviews,
            "curve": self.curve,
            "last_btc": self.last_btc,
            "applied_events": sorted(self.applied_events),
            "events": [asdict(e) for e in self.inputs.events],
            "now_us": self.inputs.now_us,
            "source_state": self.sources.source_state(),
            "termination_reason": self.termination_reason,
            "cramer_last_fetch": self.sources.cramer.last_fetch.isoformat()
            if self.sources.cramer.last_fetch
            else None,
            "gamma_exit_context": self.sources.gamma_source.latest_exit_context,
            "gamma_state": self.sources.gamma_state(),
            "jev_rows": [
                list(r) for r in cached.db.execute("SELECT * FROM jev_exit_reviews ORDER BY id")
            ]
            if cached
            else None,
        }
        return {"payload": json.loads(canonical(payload)), "sha256": digest(payload)}

    @classmethod
    def restore(cls, checkpoint, *, model_cache=None):
        from datetime import datetime

        payload = checkpoint["payload"]
        if (
            checkpoint["sha256"] != digest(payload)
            or payload["schema"] != "liquid-portfolio-checkpoint-v1"
        ):
            raise ValueError("portfolio_checkpoint_integrity_failure")
        if payload["jev_rows"] is not None and model_cache is None:
            raise ValueError("same_model_cache_required_for_restart")
        engine = cls(
            SimulatedExecution.restore(payload["simulation"]),
            policy=payload["policy"],
            enabled=payload["enabled"],
            mode=payload["mode"],
            margin_mode=payload["margin_mode"],
            latency_us=payload["latency_us"],
            model_cache=model_cache,
        )
        engine.sources.restore_source_state(payload["source_state"])
        engine.inputs.advance(
            payload["now_us"],
            [Event(**{**e, "lineage": tuple(e["lineage"])}) for e in payload["events"]],
        )
        for key in ("plans", "decisions", "reviews", "curve", "last_btc", "termination_reason"):
            setattr(engine, key, json.loads(canonical(payload[key])))
        engine.consumed, engine.applied_events = (
            set(payload["consumed"]),
            set(payload["applied_events"]),
        )
        stamp = payload["cramer_last_fetch"]
        engine.sources.cramer.last_fetch = datetime.fromisoformat(stamp) if stamp else None
        engine.sources.restore_gamma_state(payload["gamma_state"])
        if engine.jev.cached:
            engine.jev.cached.db.executemany(
                "INSERT INTO jev_exit_reviews VALUES(?,?,?,?,?,?,?,?)", payload["jev_rows"]
            )
        return engine
