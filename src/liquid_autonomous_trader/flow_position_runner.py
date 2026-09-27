"""Persistent Flow exits over acknowledged net-position lifecycle evidence."""

import hashlib
import json
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from liquid_autonomous_trader.frozen.flow_mirror import (
    FLOW_MIRROR_V1_ALLOWED_SYMBOLS,
    FlowMirrorConfigV1,
    FlowMirrorExitAction,
    FlowMirrorPositionStage,
    FlowMirrorPositionV1,
    evaluate_flow_mirror_exit,
    leveraged_price_roi_pct,
)
from liquid_autonomous_trader.frozen.models import SignalDirection
from liquid_autonomous_trader.liquid_operations import LiquidOperationBlocked
from liquid_autonomous_trader.local_accounting import AccountingError


class FlowPositionRunner:
    def __init__(self, lifecycle, funding, *, clock):
        self.lifecycle, self.funding, self.clock = lifecycle, funding, clock
        self.db = lifecycle.risk_store.db
        self.funding_unavailable = set()
        self.config = FlowMirrorConfigV1(allowed_symbols=FLOW_MIRROR_V1_ALLOWED_SYMBOLS)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS flow_runtime_positions("
            "owner TEXT PRIMARY KEY,body TEXT NOT NULL,quantity_step TEXT NOT NULL,"
            "price_step TEXT NOT NULL,pending TEXT)"
        )

    def register(self, owner, *, delivery_id, quantity_step, price_step):
        record, _, _, evidence = self.lifecycle._owned(owner)
        if record["strategy"] != "flow_show_mirror" or not evidence.protection_verified:
            raise LiquidOperationBlocked("flow_owned_protected_entry_required")
        for step in (quantity_step, price_step):
            if not isinstance(step, Decimal) or not step.is_finite() or step <= 0:
                raise LiquidOperationBlocked("flow_native_steps_required")
        p = evidence.position
        existing = self.db.execute(
            "SELECT body FROM flow_runtime_positions WHERE owner=?", (owner,)
        ).fetchone()
        if existing:
            old = FlowMirrorPositionV1.model_validate_json(existing[0])
            if old.delivery_event_id != delivery_id or old.average_entry_price != p.entry_price:
                raise LiquidOperationBlocked("flow_runner_registration_conflict")
            return
        funding = self.funding.cached_usage(owner)
        if funding is None:
            self.funding_unavailable.add(owner)
        position = FlowMirrorPositionV1(
            position_id=owner,
            delivery_event_id=delivery_id,
            symbol=p.broker_symbol,
            direction=p.side,
            opened_at=self.clock(),
            average_entry_price=p.entry_price,
            initial_quantity=p.quantity,
            remaining_quantity=p.quantity,
            leverage=p.leverage,
            stage=FlowMirrorPositionStage.INITIAL,
            current_stop_roi_pct=leveraged_price_roi_pct(
                direction=SignalDirection(p.side),
                entry_price=p.entry_price,
                current_price=p.stop_price,
                leverage=p.leverage,
            ),
            high_water_roi_pct=Decimal(0),
            funding_reserve_usd=(funding["reserve_usd"] if funding else Decimal(0)),
        )
        if p.quantity % quantity_step:
            raise LiquidOperationBlocked("flow_observed_quantity_precision_invalid")
        self.db.execute(
            "INSERT INTO flow_runtime_positions VALUES(?,?,?,?,NULL)",
            (owner, position.model_dump_json(), str(quantity_step), str(price_step)),
        )

    def tick(self, owner):
        self.funding_unavailable.discard(owner)
        row = self.db.execute(
            "SELECT * FROM flow_runtime_positions WHERE owner=?", (owner,)
        ).fetchone()
        if row is None:
            raise LiquidOperationBlocked("flow_runner_state_missing")
        state = FlowMirrorPositionV1.model_validate_json(row["body"])
        record = self.lifecycle.risk_store.execution(owner)
        if (
            record is not None
            and record["state"] == "closed"
            and not self.lifecycle.journal.db.execute(
                "SELECT 1 FROM liquid_market_owners WHERE owner_intent=?", (owner,)
            ).fetchone()
        ):
            self._save(
                owner,
                state.model_copy(
                    update={
                        "stage": FlowMirrorPositionStage.CLOSED,
                        "remaining_quantity": Decimal(0),
                    }
                ),
            )
            return "flat_reconciled"
        evidence = self.lifecycle.reconcile(owner)
        pending = json.loads(row["pending"]) if row["pending"] else None
        if any(
            o["state"] != "acknowledged" or o["kind"] != "entry"
            for o in self.lifecycle.journal.unresolved()
        ):
            return "unknown_write_reconciliation_only"
        p = evidence.position
        if p is None:
            self._save(
                owner,
                state.model_copy(
                    update={
                        "stage": FlowMirrorPositionStage.CLOSED,
                        "remaining_quantity": Decimal(0),
                    }
                ),
            )
            return "flat_reconciled"
        if p.entry_price != state.average_entry_price or p.side != state.direction.value:
            raise LiquidOperationBlocked("flow_position_identity_changed")
        if not evidence.protection_verified:
            self.lifecycle.close_owned(owner, request_id=owner + ":protection-recovery")
            return "unprotected_position_reduced"
        if pending:
            operation = self.lifecycle.journal.operation(pending["request_id"])
            if operation is None:
                # No journal entry proves no POST was attempted. Restore the last
                # acknowledged stage, then reconsider the exit against fresh prices.
                state = state.model_copy(
                    update={
                        "stage": (
                            FlowMirrorPositionStage.RUNNER
                            if state.tp1_filled_quantity > 0
                            else FlowMirrorPositionStage.INITIAL
                        )
                    }
                )
                self._save(owner, state)
                return "pre_dispatch_management_recompute"
            if operation["state"] != "retired":
                raise LiquidOperationBlocked("flow_pending_management_not_retired")
            if pending["action"] == "tp1":
                reduction = Decimal(pending.get("quantity", str(state.initial_quantity / 2)))
                if not 0 < reduction < state.initial_quantity:
                    raise LiquidOperationBlocked("flow_tp1_pending_quantity_invalid")
                if p.quantity != state.initial_quantity - reduction:
                    raise LiquidOperationBlocked("flow_tp1_net_quantity_mismatch")
                # Acknowledged native reduction with exact protected net readback;
                # this is not a synthetic exchange cumulative-fill receipt.
                state = state.model_copy(
                    update={
                        "stage": FlowMirrorPositionStage.RUNNER,
                        "remaining_quantity": p.quantity,
                        "tp1_filled_quantity": reduction,
                    }
                )
            elif pending["action"] == "stop":
                if p.stop_price != Decimal(pending["stop"]):
                    raise LiquidOperationBlocked("flow_pending_stop_not_observed")
            self._save(owner, state)
        if p.quantity != state.remaining_quantity:
            # Reconstruct any number of acknowledged Jev partials, including a
            # crash between broker confirmation and updating the Flow projection.
            rows = self.lifecycle.journal.db.execute(
                "SELECT r.*,o.state FROM liquid_partial_reductions r JOIN liquid_operations o "
                "ON o.request_id=r.request_id WHERE r.owner_intent=?",
                (owner,),
            ).fetchall()
            if not rows or any(r["state"] != "retired" for r in rows):
                raise LiquidOperationBlocked("flow_unexpected_net_quantity")
            total = sum((Decimal(r["requested_quantity"]) for r in rows), Decimal(0))
            jev_reductions = [r for r in rows if r["request_id"].startswith(owner + ":jev:")]
            if (
                not jev_reductions
                or state.initial_quantity - total != p.quantity
                or any(
                    r["before_side"] != p.side or Decimal(r["before_entry"]) != p.entry_price
                    for r in rows
                )
            ):
                raise LiquidOperationBlocked("flow_unexpected_net_quantity")
            state = state.model_copy(
                update={
                    "remaining_quantity": p.quantity,
                    "tp1_filled_quantity": total,
                    "stage": FlowMirrorPositionStage.RUNNER,
                }
            )
            self._save(owner, state)
        funding_available = True
        try:
            self.funding.bind(owner, symbol=p.broker_symbol, side=p.side)
            usage = self.funding.observe(owner)
        except AccountingError:
            # Keep native stops and price-based management available during an
            # evidence outage; never invent a zero funding observation.
            funding_available = False
            self.funding_unavailable.add(owner)
            usage = self.funding.cached_usage(owner)
        stop_roi = leveraged_price_roi_pct(
            direction=state.direction,
            entry_price=p.entry_price,
            current_price=p.stop_price,
            leverage=p.leverage,
        )
        state = state.model_copy(
            update={
                **({"adverse_funding_usd": usage["consumed_usd"]} if usage else {}),
                "current_stop_roi_pct": stop_roi,
                "high_water_roi_pct": max(
                    state.high_water_roi_pct,
                    leveraged_price_roi_pct(
                        direction=state.direction,
                        entry_price=p.entry_price,
                        current_price=p.mark_price,
                        leverage=p.leverage,
                    ),
                ),
            }
        )
        if getattr(self, "jev", None) is not None:
            plan_row = self.db.execute(
                "SELECT plan FROM sleeve_runtime_decisions WHERE intent_id=?", (owner,)
            ).fetchone()
            if plan_row:
                self._save(owner, state)
                result = self.jev.manage(owner, json.loads(plan_row[0]), evidence)
                if result is not None:
                    return result
            if getattr(self.jev, "stop_focused", False):
                self._save(owner, state)
                return "jev_stop_preserved"
        # TP1 requires a verified breakeven stop before the runner can continue.
        if (
            getattr(self, "jev", None) is None
            and state.stage is FlowMirrorPositionStage.RUNNER
            and stop_roi < 0
        ):
            return self._stop(owner, state, p.entry_price, Decimal(row["price_step"]))
        decision = evaluate_flow_mirror_exit(
            config=self.config,
            position=state,
            current_price=p.mark_price,
            funding_exit_enabled=False,
        )
        if decision.action is FlowMirrorExitAction.HOLD:
            self._save(owner, decision.updated_position)
            return "protected_hold" if funding_available else "funding_unavailable_protected_hold"
        if decision.action is FlowMirrorExitAction.CLOSE_FULL:
            request = owner + ":exit:" + decision.reasons[0]
            self._save(owner, decision.updated_position, {"request_id": request, "action": "close"})
            self.lifecycle.close_owned(owner, request_id=request)
            return decision.reasons[0]
        if decision.action is FlowMirrorExitAction.REDUCE_TP1:
            step = Decimal(row["quantity_step"])
            quantity = (decision.target_quantity / step).to_integral_value(
                rounding=ROUND_FLOOR
            ) * step
            if (
                quantity <= 0
                or quantity >= p.quantity
                or quantity * p.mark_price < 10
                or (p.quantity - quantity) * p.mark_price < 10
            ):
                request = owner + ":tp1-full"
                self._save(
                    owner,
                    decision.updated_position,
                    {"request_id": request, "action": "close"},
                )
                self.lifecycle.close_owned(owner, request_id=request)
                return "tp1_full_close_below_partial_minimum"
            request = owner + ":tp1"
            self._save(
                owner,
                decision.updated_position,
                {"request_id": request, "action": "tp1", "quantity": str(quantity)},
            )
            self.lifecycle.reduce_owned(
                owner,
                request_id=request,
                quantity=quantity,
                quantity_step=Decimal(row["quantity_step"]),
            )
            return "tp1_observed_pending_runner_stop"
        if getattr(self, "jev", None) is not None:
            self._save(owner, decision.updated_position)
            return "jev_stop_preserved"
        return self._stop(
            owner,
            decision.updated_position,
            decision.proposed_stop_price,
            Decimal(row["price_step"]),
        )

    def _stop(self, owner, state, price, step):
        # Round toward a tighter stop. Lifecycle independently forbids loosening.
        stop = (price / step).to_integral_value(
            rounding=ROUND_CEILING if state.direction.value == "long" else ROUND_FLOOR
        ) * step
        request = owner + ":flow-stop:" + hashlib.sha256(str(stop).encode()).hexdigest()[:16]
        _, _, _, evidence = self.lifecycle._owned(owner)
        p = evidence.position
        if p is None:
            return "flat_reconciled"
        if (p.side == "long" and stop >= p.mark_price) or (
            p.side == "short" and stop <= p.mark_price
        ):
            request = owner + ":flow-stop-already-crossed"
            self._save(owner, state, {"request_id": request, "action": "close"})
            self.lifecycle.close_owned(owner, request_id=request)
            return "computed_trailing_stop_already_crossed"
        self._save(owner, state, {"request_id": request, "action": "stop", "stop": str(stop)})
        self.lifecycle.tighten_stop(owner, request_id=request, stop=stop)
        return "flow_stop_tightened"

    def _save(self, owner, state, pending=None):
        self.db.execute(
            "UPDATE flow_runtime_positions SET body=?,pending=? WHERE owner=?",
            (
                state.model_dump_json(),
                json.dumps(pending, sort_keys=True) if pending else None,
                owner,
            ),
        )
