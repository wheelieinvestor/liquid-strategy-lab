"""Bounded asynchronous Jev reviews; only the existing fenced executor writes orders."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

from liquid_autonomous_trader import jev_exit_policy as legacy_policy
from liquid_autonomous_trader.jev_exit_policy import (
    INPUT_USD_PER_MILLION,
    MODEL,
    MONTHLY_BUDGET,
    REQUEST_RESERVE,
    THRESHOLDS,
)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def position_state(p):
    return {
        "symbol": p.broker_symbol,
        "side": p.side,
        "entry": str(p.entry_price),
        "quantity": str(p.quantity),
        "stop": str(p.stop_price),
        "leverage": str(p.leverage),
    }


def ask_jev(state, api_key):
    raise RuntimeError("live_inference_not_available_use_exact_recorded_cache")


class JevExitManager:
    def __init__(
        self,
        lifecycle,
        market,
        *,
        api_key,
        clock=lambda: datetime.now(UTC),
        ask=ask_jev,
        policy=legacy_policy,
    ):
        self.policy = policy
        self.stop_focused = getattr(policy, "STOP_FOCUSED", False)
        self.lifecycle, self.market, self.clock = lifecycle, market, clock
        self.api_key, self.ask = api_key, ask
        self.db = lifecycle.risk_store.db
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jev-exit")
        self.pending = {}
        self.input_failures = {}
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jev_exit_positions(
                owner TEXT PRIMARY KEY, envelope TEXT NOT NULL, peak_r TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jev_exit_reviews(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, started_at TEXT NOT NULL,
                month TEXT NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL,
                response TEXT, outcome TEXT, latency_ms REAL, cost_usd TEXT NOT NULL,
                request_id TEXT UNIQUE, policy_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jev_exit_review_owner ON jev_exit_reviews(owner,started_at);
        """)
        # In-flight inference may have been billed: keep its reservation. Never
        # replay a broker action from a saved model response after a restart.
        self.db.execute(
            "UPDATE jev_exit_reviews SET state='fallback',outcome='restart_interrupted' "
            "WHERE state='pending'"
        )

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def retire_closed_reviews(self):
        for owner, (review, future, _) in list(self.pending.items()):
            record = self.lifecycle.risk_store.execution(owner)
            if record and record["state"] == "closed" and future.done():
                result = future.result()
                self.db.execute(
                    "UPDATE jev_exit_reviews SET snapshot=?,response=?,latency_ms=?,"
                    "cost_usd=? WHERE id=?",
                    (
                        encoded(result["snapshot"]),
                        encoded(result["response"]),
                        result["latency_ms"],
                        result["cost"],
                        review,
                    ),
                )
                self._finish(review, "discarded", "position_closed_during_review")
                del self.pending[owner]
        for row in self.db.execute(
            "SELECT DISTINCT owner FROM jev_exit_reviews WHERE state='executing'"
        ).fetchall():
            self._recover_actions(row[0])

    def budget(self):
        month = self.clock().astimezone(UTC).strftime("%Y-%m")
        used = sum(
            (
                Decimal(r[0])
                for r in self.db.execute(
                    "SELECT cost_usd FROM jev_exit_reviews WHERE month=?", (month,)
                )
            ),
            Decimal(0),
        )
        return month, used

    def report(self):
        month, used = self.budget()
        rows = self.db.execute("SELECT * FROM jev_exit_reviews ORDER BY started_at DESC LIMIT 25")
        recent = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item["snapshot"])
            item["response"] = json.loads(item["response"]) if item["response"] else None
            recent.append(item)
        counts = dict(self.db.execute("SELECT state,COUNT(*) FROM jev_exit_reviews GROUP BY state"))
        return {
            "enabled": True,
            "observed_at": self.clock().isoformat(),
            "configured": bool(self.api_key),
            "model": MODEL,
            "criteria_version": self.policy.VERSION,
            "decision_thresholds": THRESHOLDS,
            "exposure_policy": self.policy.REDUCTION_POLICY,
            "stop_authority": "jev",
            "stop_fallback": "preserve_verified_broker_stop",
            "position_fallback": "preserve_quantity"
            if self.stop_focused
            else "legacy_quantity_exits",
            "policy_hash": self.policy.POLICY_HASH,
            "month": month,
            "spent_or_reserved_usd": str(used),
            "monthly_budget_usd": str(MONTHLY_BUDGET),
            "pending_reviews": len(self.pending),
            "review_counts": counts,
            "input_failures": dict(self.input_failures),
            "recent": recent,
        }

    def _envelope(self, owner, plan, p):
        row = self.db.execute(
            "SELECT envelope FROM jev_exit_positions WHERE owner=?", (owner,)
        ).fetchone()
        if row:
            envelope = json.loads(row[0])
            if any(envelope[k] != position_state(p)[k] for k in ("symbol", "side", "entry")):
                raise ValueError("jev_position_identity_changed")
            return envelope
        record = self.lifecycle.risk_store.execution(owner)
        opened = self.lifecycle.journal.db.execute(
            "SELECT MIN(started_at) FROM liquid_operations WHERE owner_intent=? "
            "AND kind='entry' AND state IN ('acknowledged','retired')",
            (owner,),
        ).fetchone()[0]
        if not opened or record["state"] != "open":
            raise ValueError("jev_original_entry_missing")
        original = Decimal(record["initial_stop"])
        direction = 1 if p.side == "long" else -1
        risk = direction * (p.entry_price - original)
        if risk <= 0:
            raise ValueError("jev_original_risk_invalid")
        envelope = {
            **position_state(p),
            "original_stop": str(original),
            "risk_price": str(risk),
            "opened_at": opened,
            "entry_plan": plan,
            "strategy": record["strategy"],
            "price_risk_budget": str(
                Decimal(record["planned_loss"]) - Decimal(record["stressed_cost"])
            ),
            "excursion_basis": "observed_broker_marks_since_jev_enrollment_not_lifetime_peak",
        }
        self.db.execute(
            "INSERT INTO jev_exit_positions VALUES(?,?,?)",
            (owner, encoded(envelope), str(direction * (p.mark_price - p.entry_price) / risk)),
        )
        return envelope

    def _recover_actions(self, owner):
        for row in self.db.execute(
            "SELECT id,request_id,outcome FROM jev_exit_reviews "
            "WHERE owner=? AND state='executing'",
            (owner,),
        ).fetchall():
            op = self.lifecycle.journal.operation(row["request_id"])
            if op is None:
                self._finish(row["id"], "fallback", "action_not_dispatched")
            elif op["state"] == "retired":
                # Keep the pre-dispatch action: repeat-partial guards and audit
                # counts depend on it after a crash between broker and review commits.
                self._finish(row["id"], "applied", row["outcome"])

    def _finish(self, review, state, outcome):
        self.db.execute(
            "UPDATE jev_exit_reviews SET state=?,outcome=? WHERE id=?", (state, outcome, review)
        )

    def _work(self, base):
        started = time.monotonic()
        state = base.copy()
        cost = Decimal(0)
        response = None
        try:
            market = self.market(state["position"]["symbol"])
            state["market"] = market
            p = state["position"]
            direction = 1 if p["side"] == "long" else -1
            price = Decimal(market["bid"] if direction == 1 else market["ask"])
            if direction * (price - Decimal(p["stop"])) <= 0:
                raise ValueError("jev_existing_stop_crossed")
            risk = Decimal(state["original"]["risk_price"])
            state["current_r"] = str(direction * (price - Decimal(p["entry"])) / risk)
            state["giveback_r"] = str(
                max(Decimal(0), Decimal(state["peak_r"]) - Decimal(state["current_r"]))
            )
            state["stop_candidates"], state["reduction_quantities"] = self.policy.candidates(
                p,
                market,
                state["original"]["original_stop"],
                **({"strategy": state["strategy"]} if self.stop_focused else {}),
            )
            budget = Decimal(state["original"]["price_risk_budget"])
            maintenance = 1 / (2 * Decimal(market["maximum_leverage"]))
            liquidation_distance = (
                Decimal(p["entry"]) * (1 / Decimal(p["leverage"]) - maintenance) / (1 + maintenance)
            )
            state["stop_candidates"] = {
                k: v
                for k, v in state["stop_candidates"].items()
                if max(Decimal(0), direction * (Decimal(p["entry"]) - Decimal(v)))
                * Decimal(p["quantity"])
                <= budget
                and (
                    state["strategy"] == "btc_momentum"
                    or max(Decimal(0), direction * (Decimal(p["entry"]) - Decimal(v))) * 3
                    <= liquidation_distance
                )
            }
            state["stop_candidate_effects"] = {
                key: {
                    "price": value,
                    "change": "tighten"
                    if direction * (Decimal(value) - Decimal(p["stop"])) > 0
                    else "loosen",
                    "distance_from_quote_atr": str(
                        direction * (price - Decimal(value)) / Decimal(market["atr14_price"])
                    ),
                }
                for key, value in state["stop_candidates"].items()
            }
            state["current_stop_distance_atr"] = str(
                direction * (price - Decimal(p["stop"])) / Decimal(market["atr14_price"])
            )
            state["allowed_actions"] = [
                "HOLD",
                "CLOSE_ALL",
                "ABSTAIN",
                *state["reduction_quantities"],
            ]
            levels = {k: market[k] for k in ("support_prior20", "resistance_prior20")}
            if state["plan"].get("target"):
                levels["legacy_target"] = state["plan"]["target"]
            state["level_distances_r"] = {
                k: str(direction * (Decimal(v) - price) / risk) for k, v in levels.items()
            }
            state["remaining_notional_usd"] = str(Decimal(p["quantity"]) * price)
            state["price_pnl_usd"] = str(
                direction * (price - Decimal(p["entry"])) * Decimal(p["quantity"])
            )
            state["remaining_price_risk_usd"] = str(
                max(Decimal(0), direction * (Decimal(p["entry"]) - Decimal(p["stop"])))
                * Decimal(p["quantity"])
            )
            if self.stop_focused:
                self.policy.enrich(state)
            if len(encoded(state).encode()) > 24000:
                raise ValueError("jev_context_too_large")
            cost = REQUEST_RESERVE  # unknown responses retain conservative billing reservation
            before_model = time.monotonic()
            response = json.loads(encoded(self.ask(state, self.api_key)))
            latency = (time.monotonic() - before_model) * 1000
            tokens = response.get("usage", {}).get("input_tokens")
            if type(tokens) is int and 0 <= tokens <= 64000:
                cost = Decimal(tokens) * INPUT_USD_PER_MILLION / 1000000
            if latency > 1500:
                raise TimeoutError("jev_model_deadline")
            if response.get("model") != MODEL:
                raise ValueError("jev_model_mismatch")
            return {
                "snapshot": state,
                "response": response,
                "cost": str(cost),
                "latency_ms": latency,
                "error": None,
            }
        except Exception as exc:
            # Exception text and HTTP objects may contain credentials or raw content.
            return {
                "snapshot": state,
                "response": response,
                "cost": str(cost),
                "latency_ms": (time.monotonic() - started) * 1000,
                "error": "input_unavailable" if cost == 0 else type(exc).__name__,
            }

    def manage(self, owner, plan, evidence, *, context=None):
        result = self._manage(owner, plan, evidence, context=context)
        if (
            self.stop_focused
            and evidence.position is not None
            and evidence.protection_verified
            and result is None
        ):
            return "jev_stop_preserved"
        return result

    def _manage(self, owner, plan, evidence, *, context=None):
        """Return None to run legacy exits. Pending/accepted Jev reviews own discretion."""
        p = evidence.position
        if p is None or not evidence.protection_verified:
            return None  # caller retains its mandatory safety path
        self._recover_actions(owner)
        try:
            envelope = self._envelope(owner, plan, p)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.input_failures[owner] = "original_position_or_risk_unavailable_using_legacy"
            return None
        self.input_failures.pop(owner, None)
        risk = Decimal(envelope["risk_price"])
        direction = 1 if p.side == "long" else -1
        current_r = direction * (p.mark_price - p.entry_price) / risk
        peak = max(
            current_r,
            Decimal(
                self.db.execute(
                    "SELECT peak_r FROM jev_exit_positions WHERE owner=?", (owner,)
                ).fetchone()[0]
            ),
        )
        self.db.execute("UPDATE jev_exit_positions SET peak_r=? WHERE owner=?", (str(peak), owner))
        now = self.clock()
        if owner in self.pending:
            review, future, began = self.pending[owner]
            if not future.done():
                if (now - began).total_seconds() > 15:
                    self._finish(review, "fallback", "review_deadline")
                    return None
                return "jev_review_pending"
            del self.pending[owner]
            result = future.result()
            self.db.execute(
                "UPDATE jev_exit_reviews SET snapshot=?,response=?,latency_ms=?,"
                "cost_usd=? WHERE id=?",
                (
                    encoded(result["snapshot"]),
                    encoded(result["response"]),
                    result["latency_ms"],
                    result["cost"],
                    review,
                ),
            )
            if result["error"] or (now - began).total_seconds() > 60:
                self._finish(review, "fallback", result["error"] or "review_expired")
                return None
            return self._apply(owner, review, result, p, context or {})
        last = self.db.execute(
            "SELECT * FROM jev_exit_reviews WHERE owner=? ORDER BY started_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
        context = context or {}
        if last:
            old = json.loads(last["snapshot"])
            elapsed = (now - datetime.fromisoformat(last["started_at"])).total_seconds()
            target = plan.get("target")
            crossed = bool(target and direction * (p.mark_price - Decimal(target)) >= 0)
            old_crossed = bool(
                target and direction * (Decimal(old["mark_price"]) - Decimal(target)) >= 0
            )
            material = (
                abs(current_r - Decimal(old["review_r"])) >= Decimal(".25")
                or last["outcome"] == "worker_capacity"
                or last["state"] == "superseded"
                or crossed != old_crossed
                or context.get("opposite_signal") != old["context"].get("opposite_signal")
            )
            if elapsed < 15 or (elapsed < 60 and not material):
                return None if last["state"] == "fallback" else "jev_protected_hold"
        month, used = self.budget()
        reason = (
            "key_missing"
            if not self.api_key
            else "budget_exhausted"
            if used + REQUEST_RESERVE > MONTHLY_BUDGET
            else "worker_capacity"
            if len(self.pending) >= 4
            else None
        )
        base = {
            "schema_version": self.policy.VERSION,
            "model": MODEL,
            "strategy": envelope["strategy"],
            "as_of": now.isoformat(),
            "position": position_state(p),
            "original": envelope,
            "plan": plan,
            "context": context,
            "review_r": str(current_r),
            "mark_price": str(p.mark_price),
            "peak_r": str(peak),
            "age_seconds": (now - datetime.fromisoformat(envelope["opened_at"])).total_seconds(),
            "previous": {"state": last["state"], "outcome": last["outcome"]} if last else None,
            "confirmed_reductions": [
                dict(r)
                for r in self.lifecycle.journal.db.execute(
                    "SELECT r.requested_quantity,o.completed_at FROM liquid_partial_reductions r "
                    "JOIN liquid_operations o ON o.request_id=r.request_id WHERE r.owner_intent=? "
                    "AND o.state='retired' ORDER BY o.completed_at",
                    (owner,),
                )
            ],
        }
        review = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO jev_exit_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review,
                owner,
                now.isoformat(),
                month,
                "fallback" if reason else "pending",
                encoded(base),
                None,
                reason,
                None,
                "0" if reason else str(REQUEST_RESERVE),
                owner + ":jev:" + review,
                self.policy.POLICY_HASH,
            ),
        )
        if reason:
            return None
        self.pending[owner] = (review, self.pool.submit(self._work, base), now)
        return "jev_review_pending"

    def _new_reduction_evidence(self, owner, state):
        last = self.db.execute(
            "SELECT snapshot FROM jev_exit_reviews WHERE owner=? AND state='applied' "
            "AND outcome IN ('REDUCE_25_PERCENT','REDUCE_50_PERCENT') "
            "ORDER BY started_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
        if last is None:
            return True
        old = json.loads(last[0])
        new_bar, old_bar = state["market"].get("bar_end_ms"), old["market"].get("bar_end_ms")
        return bool(
            (new_bar is not None and old_bar is not None and new_bar > old_bar)
            or Decimal(state["review_r"]) <= Decimal(old["review_r"]) - Decimal(".25")
            or (
                bool(state["context"].get("opposite_signal"))
                and state["context"].get("opposite_signal") != old["context"].get("opposite_signal")
            )
        )

    def _new_stop_evidence(self, owner, state):
        last = self.db.execute(
            "SELECT snapshot FROM jev_exit_reviews WHERE owner=? AND state='applied' "
            "AND outcome='CHANGE_STOP' ORDER BY started_at DESC LIMIT 1",
            (owner,),
        ).fetchone()
        if last is None:
            return True
        old = json.loads(last[0])
        a, b = state["market"].get("bar_end_ms"), old["market"].get("bar_end_ms")
        return bool(
            (a is not None and b is not None and a > b)
            or abs(Decimal(state["review_r"]) - Decimal(old["review_r"])) >= Decimal(".25")
        )

    def _apply(self, owner, review, result, p, context):
        state, response = result["snapshot"], result["response"]
        try:
            action, exposure_reason = self.policy.select_exposure(state, response["answers"])
            action_accepted = action is not None
            response["exposure_resolution"] = {
                "action": action,
                "reason": exposure_reason,
                "source": "executor_hierarchy",
            }
            self.db.execute(
                "UPDATE jev_exit_reviews SET response=? WHERE id=?", (encoded(response), review)
            )
            # Position and protection are independent judgments. Quantity exits take
            # precedence; an uncertain quantity answer does not veto a clear stop.
            if not action_accepted or action == "HOLD":
                selected_stop, stop_reason = self.policy.select_stop(state, response["answers"])
                if selected_stop is not None:
                    response["stop_resolution"] = {
                        "candidate": selected_stop,
                        "price": state["stop_candidates"][selected_stop],
                        "source": "executor_selected_from_jev_approved_candidates"
                        if self.stop_focused
                        else "jev_direction_and_price",
                    }
                    self.db.execute(
                        "UPDATE jev_exit_reviews SET response=? WHERE id=?",
                        (encoded(response), review),
                    )
                    action = "CHANGE_STOP"
                elif not action_accepted:
                    raise ValueError(exposure_reason + ":" + stop_reason)
            if context != state["context"]:
                self._finish(review, "superseded", "new_strategy_evidence_requires_review")
                return "jev_review_superseded"
            if (
                "bar_end_ms" in state["market"]
                and state["market"]["bar_end_ms"] + 1
                != int((self.clock().timestamp() - 2) // 900) * 900000
            ):
                self._finish(review, "superseded", "new_closed_bar_requires_review")
                return "jev_review_superseded"
            if position_state(p) != state["position"]:
                raise ValueError("position_or_context_changed")
            # Current native quote is re-read by the executor. The worker only
            # reads market/model APIs; every broker write stays on this thread.
            quote = self.market.quote(p.broker_symbol)
            direction = 1 if p.side == "long" else -1
            price = Decimal(quote["bid"] if direction == 1 else quote["ask"])
            old_price = Decimal(
                state["market"]["bid"] if direction == 1 else state["market"]["ask"]
            )
            if (
                abs(price - old_price) > Decimal(state["original"]["risk_price"]) * Decimal(".1")
                or direction * (price - p.stop_price) <= 0
            ):
                raise ValueError("quote_changed_or_stop_crossed")
            if action == "HOLD":
                self._finish(review, "applied", "HOLD")
                return "jev_hold"
            stop = None
            quantity = None
            if action == "CHANGE_STOP":
                if self.stop_focused and not self._new_stop_evidence(owner, state):
                    self._finish(review, "suppressed", "stop_already_adjusted_to_evidence")
                    return "jev_stop_preserved"
                stop = Decimal(state["stop_candidates"][selected_stop])
                if direction * (price - stop) <= 0 or stop % Decimal(quote["price_step"]):
                    raise ValueError("selected_stop_crossed")
            if action in state["reduction_quantities"]:
                if not self._new_reduction_evidence(owner, state):
                    self._finish(review, "suppressed", "partial_already_applied_to_evidence")
                    return "jev_partial_already_applied"
                quantity = Decimal(state["reduction_quantities"][action])
                if (
                    quantity % Decimal(quote["quantity_step"])
                    or min(quantity, p.quantity - quantity) * price < 10
                ):
                    raise ValueError("partial_minimum_changed")
        except Exception as exc:
            reasons = {
                "uncertain_stop_direction",
                "uncertain_stop_price",
                "stop_kept",
                "stop_abstained",
                "no_suitable_stop_price",
                "position_or_context_changed",
                "quote_changed_or_stop_crossed",
                "uncertain_stop",
                "selected_stop_crossed",
                "partial_minimum_changed",
            }
            exposure_reasons = {
                "uncertain_exposure",
                "exposure_abstained",
                "reduction_below_partial_minimum",
            }
            reasons |= {a + ":" + b for a in exposure_reasons for b in reasons.copy()}
            reason = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in reasons
                else "invalid_response"
            )
            self._finish(review, "fallback", reason)
            return None
        request = owner + ":jev:" + review
        expected = {
            **state["position"],
            "decision_quote": {
                "price": str(price),
                "observed_at": quote["observed_at"],
                "risk_price": state["original"]["risk_price"],
            },
        }
        self.db.execute(
            "UPDATE jev_exit_reviews SET state='executing',outcome=? WHERE id=?", (action, review)
        )
        # From here failures propagate: a possibly dispatched write MUST NOT
        # cause a second discretionary action via fallback in the same cycle.
        if action == "CLOSE_ALL":
            self.lifecycle.close_owned(owner, request_id=request, expected_position=expected)
        elif quantity is not None:
            self.lifecycle.reduce_owned(
                owner,
                request_id=request,
                quantity=quantity,
                quantity_step=Decimal(quote["quantity_step"]),
                expected_position=expected,
            )
        else:
            self.lifecycle.replace_stop_bounded(
                owner,
                request_id=request,
                stop=stop,
                expected_position=expected,
                maximum_leverage=Decimal(quote["maximum_leverage"]),
            )
        self._finish(review, "applied", action)
        return "jev_" + action.lower()
