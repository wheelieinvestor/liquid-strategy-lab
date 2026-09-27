"""Restore falsely-flat Flow lifecycles from exact independent fill evidence."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from liquid_autonomous_trader.liquid_operations import LiquidOperationBlocked
from liquid_autonomous_trader.live_policy import Strategy, reserved_price_loss_limit


def recover_closed_flow(lifecycle, funding):
    venue = getattr(lifecycle.reconciler, "venue", None)
    if venue is None:
        return []
    risk, journal = lifecycle.risk_store, lifecycle.journal
    risk.db.execute(
        "CREATE TABLE IF NOT EXISTS closed_position_recoveries("
        "intent_id TEXT PRIMARY KEY,proof_hash TEXT NOT NULL,completed INTEGER NOT NULL)"
    )
    recovered = []
    # A completed recovery is never applied twice. The original closed row stays
    # in the append-only lifecycle history alongside the corrective observation.
    rows = risk.db.execute(
        "SELECT e.* FROM executions e LEFT JOIN closed_position_recoveries r "
        "USING(intent_id) WHERE e.strategy='flow_show_mirror' "
        "AND e.state='closed' AND (r.completed IS NULL OR r.completed=0)"
    ).fetchall()
    for record in rows:
        owner, symbol = record["intent_id"], record["symbol"]
        pending = risk.db.execute(
            "SELECT * FROM closed_position_recoveries WHERE intent_id=?", (owner,)
        ).fetchone()
        # A later, owned lifecycle manages this market normally. Historical
        # closed entries must not interfere with it or claim its position.
        if (
            pending is None
            and risk.db.execute(
                "SELECT 1 FROM executions WHERE symbol=? AND intent_id!=? "
                "AND state IN ('reserved','submitting','unknown','accepted',"
                "'partially_filled','open')",
                (symbol, owner),
            ).fetchone()
        ):
            continue
        account, orders, evidence = lifecycle.reconciler.position(symbol)
        p = evidence.position
        if p is None and pending is None:
            continue
        operations = journal.db.execute(
            "SELECT * FROM liquid_operations WHERE owner_intent=?", (owner,)
        ).fetchall()
        if len(operations) != 1:
            raise LiquidOperationBlocked("closed_recovery_operation_history_ambiguous")
        op = operations[0]
        if (
            p is None
            or not evidence.protection_verified
            or op["kind"] != "entry"
            or op["account"] != "research-account"
            or op["symbol"] != symbol
            or op["state"] not in ({"retired", "acknowledged"} if pending else {"retired"})
            or not str(op["broker_order_id"]).isdigit()
            or p.side != record["side"]
            or p.quantity != Decimal(record["quantity"])
            or p.leverage != Decimal(record["leverage"])
            or p.margin_used_usd > Decimal(record["collateral"])
            or account.margin_used_usd > 500
            or account.available_collateral_usd < 150
            or p.quantity * abs(p.entry_price - Decimal(record["initial_stop"]))
            > reserved_price_loss_limit(
                Strategy(record["strategy"]),
                Decimal(record["planned_loss"]),
                Decimal(record["stressed_cost"]),
            )
            or (p.side == "long" and p.stop_price < Decimal(record["initial_stop"]))
            or (p.side == "short" and p.stop_price > Decimal(record["initial_stop"]))
        ):
            raise LiquidOperationBlocked("closed_recovery_position_or_protection_mismatch")
        other = journal.db.execute(
            "SELECT owner_intent FROM liquid_market_owners WHERE symbol=?", (symbol,)
        ).fetchone()
        if other and (not pending or other[0] != owner):
            raise LiquidOperationBlocked("closed_recovery_owner_conflict")
        if any(o["request_id"] != op["request_id"] for o in journal.unresolved()):
            raise LiquidOperationBlocked("closed_recovery_other_write_unresolved")
        if risk.db.execute(
            "SELECT 1 FROM executions WHERE symbol=? AND intent_id!=? "
            "AND state IN ('reserved','submitting','unknown','accepted','partially_filled','open')",
            (symbol, owner),
        ).fetchone():
            raise LiquidOperationBlocked("closed_recovery_other_execution")
        fills = venue.recent_fills()
        start = int(datetime.fromisoformat(op["started_at"]).timestamp() * 1000)
        end = int(datetime.fromisoformat(op["completed_at"]).timestamp() * 1000)
        same = [f for f in fills.fills if f.coin == symbol and f.time >= start - 1000]
        if (
            fills.recent_fill_limit_reached
            or not same
            or any(
                f.oid != int(op["broker_order_id"])
                or f.side != ("B" if p.side == "long" else "A")
                or not start - 1000 <= f.time <= end + 1000
                for f in same
            )
            or sum((f.sz for f in same), Decimal(0)) != p.quantity
            or sum((f.sz * f.px for f in same), Decimal(0)) / p.quantity != p.entry_price
        ):
            raise LiquidOperationBlocked("closed_recovery_independent_fills_mismatch")
        if risk.db.execute(
            "SELECT 1 FROM pending_management WHERE intent_id=?", (owner,)
        ).fetchone():
            raise LiquidOperationBlocked("closed_recovery_pending_management")
        plan = risk.db.execute(
            "SELECT plan FROM sleeve_runtime_decisions WHERE intent_id=?", (owner,)
        ).fetchone()
        if not plan:
            raise LiquidOperationBlocked("closed_recovery_exit_plan_missing")
        proof = hashlib.sha256(
            json.dumps([f.model_dump(mode="json") for f in same], sort_keys=True).encode()
        ).hexdigest()
        lifecycle._fence()
        with risk.transaction():
            prior = risk.db.execute(
                "SELECT proof_hash FROM closed_position_recoveries WHERE intent_id=?", (owner,)
            ).fetchone()
            if prior and prior[0] != proof:
                raise LiquidOperationBlocked("closed_recovery_proof_changed")
            risk.db.execute(
                "INSERT OR IGNORE INTO closed_position_recoveries VALUES(?,?,0)", (owner, proof)
            )
        # Persist intent before touching the ownership journal. A crash at either
        # boundary is revalidated and completed by this same fenced path.
        journal.db.execute("BEGIN IMMEDIATE")
        try:
            journal.db.execute(
                "INSERT OR IGNORE INTO liquid_market_owners VALUES(?,?,?)",
                ("research-account", symbol, owner),
            )
            journal.db.execute(
                "UPDATE liquid_operations SET state='acknowledged' WHERE request_id=?",
                (op["request_id"],),
            )
            journal.db.execute("COMMIT")
        except BaseException:
            journal.db.execute("ROLLBACK")
            raise
        lifecycle._fence()
        at = lifecycle.clock().isoformat()
        with risk.transaction():
            risk.db.execute(
                "UPDATE executions SET state='open',updated_at=? "
                "WHERE intent_id=? AND state='closed'",
                (at, owner),
            )
            risk.db.execute(
                "INSERT INTO lifecycle(intent_id,state,detail,at) VALUES(?,?,?,?)",
                (
                    owner,
                    "open",
                    json.dumps(
                        {
                            "reason": "false_flat_recovered_from_venue",
                            "proof_hash": proof,
                            "net_quantity": str(p.quantity),
                            "stop_order_id": evidence.stop_order_id,
                            "protection_verified": True,
                        }
                    ),
                    at,
                ),
            )
            risk.db.execute(
                "UPDATE closed_position_recoveries SET completed=1 WHERE intent_id=?", (owner,)
            )
            risk.db.execute(
                "UPDATE sleeve_runtime_decisions SET state='submitted',diagnostic=?,observed_at=? "
                "WHERE intent_id=?",
                (json.dumps({"reason": "false_flat_recovered_from_venue"}), at, owner),
            )
        if lifecycle.producer is not None:
            lifecycle.producer.record_position(
                owner, account=account, evidence=evidence, broker_order_id=op["broker_order_id"]
            )
        recovered.append(owner)
    return recovered
