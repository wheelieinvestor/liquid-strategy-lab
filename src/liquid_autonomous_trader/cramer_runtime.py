"""Cramer source inbox and recoverable position management in the shared writer."""

import hashlib
import json
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from liquid_autonomous_trader.controller import ReconciledAccount
from liquid_autonomous_trader.cramer_market import restore_markets
from liquid_autonomous_trader.cramer_models import (
    COOLDOWN,
    LEVERAGE,
    MAX_HOLD,
    MAX_NOTIONAL,
    SIGNAL_TTL,
    Classification,
    InstrumentCall,
    SourcePost,
    call_key,
    eligibility,
    fingerprint,
    inverse,
)
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy, widened_initial_stop
from liquid_autonomous_trader.shared_account_risk import shared_risk_snapshot


class CramerRuntime:
    def __init__(
        self,
        lifecycle,
        market,
        reader,
        *,
        clock,
        enabled=True,
        risk_projection=None,
    ):
        self.enabled = enabled
        self.risk_projection = risk_projection
        self.lifecycle, self.market, self.reader, self.clock = lifecycle, market, reader, clock
        self.db = lifecycle.risk_store.db
        restore_markets(lifecycle.risk_store)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cramer_v2_cursor(
                id INTEGER PRIMARY KEY,seq INTEGER NOT NULL);
            INSERT OR IGNORE INTO cramer_v2_cursor VALUES(1,0);
            CREATE TABLE IF NOT EXISTS cramer_v2_signals(
                id TEXT PRIMARY KEY,post TEXT NOT NULL,call TEXT NOT NULL,
                ticker TEXT NOT NULL,stance TEXT NOT NULL,statement_at TEXT NOT NULL,
                fingerprint TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,
                symbol TEXT,reverse_owner TEXT,observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cramer_v2_positions(
                owner TEXT PRIMARY KEY,opened_at TEXT NOT NULL,deadline TEXT NOT NULL,
                target TEXT NOT NULL,initial_risk TEXT NOT NULL,price_step TEXT NOT NULL,
                trail_active INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS cramer_v2_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,identity TEXT UNIQUE NOT NULL,
                body TEXT NOT NULL,observed_at TEXT NOT NULL);
        """)
        self.last_fetch = None

    def event(self, key, **payload):
        self.db.execute(
            "INSERT OR IGNORE INTO cramer_v2_events(identity,body,observed_at) VALUES(?,?,?)",
            (key, json.dumps(payload, sort_keys=True), self.clock().isoformat()),
        )

    def state(self, key, state, reason=None):
        self.db.execute(
            "UPDATE cramer_v2_signals SET state=?,reason=?,observed_at=? WHERE id=?",
            (state, reason, self.clock().isoformat(), key),
        )
        self.event(key + ":" + state, decision_id=key, state=state, reason=reason)

    def ingest(self):
        if self.reader is None:
            raise ValueError("cramer_source_connection_missing")
        if self.last_fetch and self.clock() - self.last_fetch < timedelta(seconds=60):
            return
        after = self.db.execute("SELECT seq FROM cramer_v2_cursor WHERE id=1").fetchone()[0]
        rows = self.reader.fetch(after)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = after
            for row in rows:
                sequence = row["seq"]
                if type(sequence) is not int or sequence < 1:
                    raise ValueError("cramer_invalid_source_sequence")
                if sequence <= after:
                    continue  # A replay cannot regress the durable cursor.
                if sequence <= previous:
                    raise ValueError("cramer_source_sequence_not_increasing")
                previous = sequence
                post = SourcePost.model_validate(row["body"]["post"])
                classification = Classification.model_validate(row["body"]["classification"])
                if classification.source_id != post.source_id:
                    raise ValueError("cramer_source_id_mismatch")
                for call in classification.calls:
                    key = call_key(post, call)
                    reason = eligibility(post, call, self.clock())
                    duplicate = self.db.execute(
                        "SELECT 1 FROM cramer_v2_signals WHERE fingerprint=? AND statement_at>=?",
                        (fingerprint(call), (call.statement_at - COOLDOWN).isoformat()),
                    ).fetchone()
                    if duplicate:
                        reason = reason or "duplicate_statement"
                    self.db.execute(
                        "INSERT OR IGNORE INTO cramer_v2_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            post.model_dump_json(),
                            call.model_dump_json(),
                            call.ticker,
                            call.stance,
                            call.statement_at.isoformat(),
                            fingerprint(call),
                            "skipped" if reason else "pending",
                            reason,
                            None,
                            None,
                            self.clock().isoformat(),
                        ),
                    )
                    self.event(
                        key + ":classified",
                        source_url=post.url,
                        ticker=call.ticker,
                        stance=call.stance,
                        confidence=call.confidence,
                        reason=reason,
                        model=row["model"],
                        prompt_sha=row["prompt_sha"],
                    )
                self.db.execute("UPDATE cramer_v2_cursor SET seq=? WHERE id=1", (row["seq"],))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.last_fetch = self.clock()

    def register_position(self, owner, plan):
        if self.db.execute("SELECT 1 FROM cramer_v2_positions WHERE owner=?", (owner,)).fetchone():
            return
        record = self.lifecycle.risk_store.execution(owner)
        # Anchor the deadline to dispatch, conservatively before the confirmed open.
        # A delayed acknowledgement/restart must never extend the one-week limit.
        opened = self.lifecycle.journal.db.execute(
            "SELECT MIN(started_at) FROM liquid_operations "
            "WHERE owner_intent=? AND kind='entry' AND state IN ('acknowledged','retired')",
            (owner,),
        ).fetchone()[0]
        if not opened or not record or record["strategy"] != Strategy.CRAMER.value:
            raise ValueError("cramer_confirmed_entry_required")
        _, _, _, evidence = self.lifecycle._owned(owner)
        p = evidence.position
        if p is None or not evidence.protection_verified:
            raise ValueError("cramer_protected_entry_required")
        risk = abs(p.entry_price - Decimal(record["initial_stop"]))
        if risk <= 0:
            raise ValueError("cramer_initial_risk_invalid")
        target = p.entry_price + risk * Decimal("1.75") * (1 if p.side == "long" else -1)
        self.db.execute(
            "INSERT INTO cramer_v2_positions VALUES(?,?,?,?,?,?,0)",
            (
                owner,
                opened,
                (datetime.fromisoformat(opened) + MAX_HOLD).isoformat(),
                str(target),
                str(risk),
                plan["price_step"],
            ),
        )

    def manage(self, owner, plan):
        evidence = self.lifecycle.reconcile(owner)
        if any(
            o["state"] != "acknowledged" or o["kind"] != "entry"
            for o in self.lifecycle.journal.unresolved()
        ):
            return "unknown_write_reconciliation_only"
        p = evidence.position
        if p is None:
            return "flat_reconciled"
        if not evidence.protection_verified:
            self.lifecycle.close_owned(owner, request_id=owner + ":protection-recovery")
            return "unprotected_position_reduced"
        self.register_position(owner, plan)
        row = self.db.execute(
            "SELECT * FROM cramer_v2_positions WHERE owner=?", (owner,)
        ).fetchone()
        if self.clock() >= datetime.fromisoformat(row["deadline"]):
            self.lifecycle.close_owned(owner, request_id=owner + ":one-week-close")
            return "one_week_time_exit"
        if getattr(self, "jev", None) is not None:
            result = self.jev.manage(
                owner,
                {**plan, "target": row["target"], "deadline": row["deadline"]},
                evidence,
                context=self.jev_context(owner),
            )
            if result is not None:
                return result
            # Stop-focused Jev uncertainty retains exposure and native protection.
            # Legacy targets cannot take over while Jev owns discretionary exits.
            return "jev_stop_preserved"
        direction = 1 if p.side == "long" else -1
        if direction * (p.mark_price - Decimal(row["target"])) >= 0:
            self.lifecycle.close_owned(owner, request_id=owner + ":target-close")
            return "target_close"
        if direction * (p.mark_price - p.entry_price) >= Decimal(row["initial_risk"]):
            self.db.execute("UPDATE cramer_v2_positions SET trail_active=1 WHERE owner=?", (owner,))
        elif not row["trail_active"]:
            return "protected_hold"
        try:
            atr = self.market.atr(p.broker_symbol)
        except Exception:
            # Missing ATR cannot suppress a later time exit or remove the native stop.
            return "protected_hold_atr_unavailable"
        tick = Decimal(row["price_step"])
        candidate = p.mark_price - direction * atr
        candidate = (candidate / tick).to_integral_value(
            rounding=ROUND_FLOOR if direction == 1 else ROUND_CEILING
        ) * tick
        if candidate <= 0 or direction * (candidate - p.stop_price) <= 0:
            return "protected_hold"
        self.lifecycle.tighten_stop(
            owner,
            request_id=owner + ":trail:" + hashlib.sha256(str(candidate).encode()).hexdigest()[:16],
            stop=candidate,
        )
        return "stop_tightened"

    def next_entry(self, runtime):
        if not self.enabled:
            return {"result": "disabled"}
        # Reversals already started must recover even if the research service is down.
        row = self.db.execute(
            "SELECT * FROM cramer_v2_signals WHERE state='closing' ORDER BY statement_at,id LIMIT 1"
        ).fetchone()
        if row is None:
            self.ingest()
            row = self.db.execute(
                "SELECT * FROM cramer_v2_signals WHERE state='pending' "
                "ORDER BY statement_at DESC,id LIMIT 1"
            ).fetchone()
        if row is None:
            return {"result": "waiting_for_signal"}
        key = row["id"]
        post, call = (
            SourcePost.model_validate_json(row["post"]),
            InstrumentCall.model_validate_json(row["call"]),
        )
        if row["state"] != "closing":
            reason = eligibility(post, call, self.clock())
            newer = self.db.execute(
                "SELECT 1 FROM cramer_v2_signals WHERE ticker=? AND "
                "statement_at>? AND (reason IS NULL OR reason='duplicate_statement') LIMIT 1",
                (call.ticker, row["statement_at"]),
            ).fetchone()
            if reason or newer:
                self.state(key, "skipped", reason or "superseded_by_newer_call")
                return {"result": "skipped", "reason": reason or "superseded_by_newer_call"}
            identity = self.market.resolve(post, call)
            # Native corroboration required before an identity is installed.
            self.market.quote(identity)
            self.lifecycle.register_cramer_market(identity)
            symbol = identity.coin
            self.db.execute("UPDATE cramer_v2_signals SET symbol=? WHERE id=?", (symbol, key))
            row = self.db.execute("SELECT * FROM cramer_v2_signals WHERE id=?", (key,)).fetchone()
            owner = self.lifecycle.journal.db.execute(
                "SELECT owner_intent FROM liquid_market_owners WHERE symbol=?", (symbol,)
            ).fetchone()
            if owner:
                record = self.lifecycle.risk_store.execution(owner[0])
                if not record or record["strategy"] != Strategy.CRAMER.value:
                    self.state(key, "skipped", "market_owned_by_other_strategy")
                    return {"result": "skipped", "reason": "market_owned_by_other_strategy"}
                if record["side"] == inverse(call):
                    self.state(key, "skipped", "same_direction_position_open")
                    return {"result": "skipped", "reason": "same_direction_position_open"}
                self.db.execute(
                    "UPDATE cramer_v2_signals SET reverse_owner=?,state='closing' WHERE id=?",
                    (owner[0], key),
                )
                row = self.db.execute(
                    "SELECT * FROM cramer_v2_signals WHERE id=?", (key,)
                ).fetchone()
        if row["state"] == "closing":
            owner = row["reverse_owner"]
            record = self.lifecycle.risk_store.execution(owner)
            if not record or record["strategy"] != Strategy.CRAMER.value:
                raise ValueError("cramer_reversal_owner_invalid")
            if record["state"] != "closed":
                self.lifecycle.reconcile(owner)
                record = self.lifecycle.risk_store.execution(owner)
                if record["state"] != "closed":
                    if getattr(self, "jev", None) is not None:
                        reason = eligibility(post, call, self.clock())
                        if reason:
                            self.state(key, "skipped", "jev_held_signal_expired")
                            return {"result": "jev_held_signal_expired"}
                        result = self.manage(owner, runtime._plan(owner))
                        # A Jev hold/reduction cannot be followed by the legacy
                        # opposite-signal close. Fallback retains that behavior.
                        if result.startswith("jev_"):
                            return {"result": result}
                        if self.lifecycle.risk_store.execution(owner)["state"] == "closed":
                            return {"result": "flat_reversal_reviewed"}
                    close_id = key + ":reverse-close"
                    if self.lifecycle.journal.operation(close_id) is not None:
                        return {"result": "reversal_close_pending_reconciliation"}
                    self.lifecycle.close_owned(owner, request_id=close_id)
            if self.lifecycle.risk_store.execution(owner)["state"] != "closed":
                return {"result": "reversal_close_pending_reconciliation"}
            reason = eligibility(post, call, self.clock())
            if reason:
                self.state(key, "skipped", "reversal_closed_signal_expired")
                return {"result": "flat_signal_expired"}
        if row["state"] == "closing":
            self.ingest()
            newer = self.db.execute(
                "SELECT 1 FROM cramer_v2_signals WHERE ticker=? AND statement_at>? "
                "AND reason IS NULL LIMIT 1",
                (call.ticker, row["statement_at"]),
            ).fetchone()
            if newer:
                self.state(key, "skipped", "reversal_superseded_after_close")
                return {"result": "flat_superseded"}
        # Re-resolve after close: a cached entry quote cannot authorize a reversal.
        identity = self.market.resolve(post, call)
        if row["symbol"] is not None and identity.coin != row["symbol"]:
            raise ValueError("cramer_reversal_market_changed")
        if row["state"] != "closing":
            last = self.db.execute(
                "SELECT MAX(p.opened_at) FROM cramer_v2_positions p JOIN executions e "
                "ON e.intent_id=p.owner WHERE e.symbol=?",
                (identity.coin,),
            ).fetchone()[0]
            if last and self.clock() - datetime.fromisoformat(last) < COOLDOWN:
                self.state(key, "skipped", "ticker_cooldown_active")
                return {"result": "skipped", "reason": "ticker_cooldown_active"}
        atr = self.market.atr(identity.coin)
        quote = self.market.quote(identity)
        self.lifecycle.register_cramer_market(identity)
        bundle = self.prepare(key, post, call, quote, atr)
        result = runtime._entry(bundle)
        self.state(key, "consumed", result["result"])
        return result

    def jev_context(self, owner):
        rows = self.db.execute(
            "SELECT * FROM cramer_v2_signals WHERE reverse_owner=? "
            "AND state='closing' ORDER BY statement_at DESC LIMIT 1",
            (owner,),
        ).fetchall()
        if not rows:
            return {"opposite_signal": None}
        row = rows[0]
        post = SourcePost.model_validate_json(row["post"])
        call = InstrumentCall.model_validate_json(row["call"])
        if eligibility(post, call, self.clock()):
            return {"opposite_signal": None}
        return {
            "opposite_signal": {
                "id": row["id"],
                "statement_at": row["statement_at"],
                "stance": call.stance,
                "evidence": call.evidence,
            }
        }

    def prepare(self, key, post, call, quote, atr):
        reason = eligibility(post, call, self.clock())
        if reason:
            raise ValueError("cramer_" + reason)
        if (quote.ask - quote.bid) / quote.midpoint * 10000 > 25:
            raise ValueError("cramer_spread_too_wide")
        if min(quote.bid_notional, quote.ask_notional) < MAX_NOTIONAL * 5:
            raise ValueError("cramer_insufficient_depth")
        side = inverse(call)
        entry = quote.ask if side == "long" else quote.bid
        quantity = (MAX_NOTIONAL / entry / quote.quantity_step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * quote.quantity_step
        notional = quantity * entry
        distance = max(Decimal("1.25") * atr, 3 * (quote.ask - quote.bid))
        proposed = entry + distance * (-1 if side == "long" else 1)
        widened = widened_initial_stop(entry, proposed, side)
        tick = max(
            Decimal(1).scaleb(-6) / quote.quantity_step, Decimal(1).scaleb(widened.adjusted() - 4)
        )
        maintenance = 1 / (2 * quote.maximum_leverage)
        liquidation = entry * (1 / LEVERAGE - maintenance) / (1 + maintenance)
        a, orders, evidence = self.lifecycle.reconciler.position(
            quote.identity.coin, all_positions=True
        )
        if evidence.position or evidence.working_order_ids:
            raise ValueError("cramer_entry_market_not_flat")
        risk = (self.risk_projection or shared_risk_snapshot)(
            self.lifecycle.reconciler.accounting_projection,
            self.lifecycle.reconciler.before_account,
            a,
            orders,
            journal=self.lifecycle.journal,
            executions=self.lifecycle.risk_store,
            now=self.clock(),
        )
        if not 0 <= (self.clock() - quote.observed_at).total_seconds() <= 5:
            raise ValueError("cramer_quote_expired_during_reconciliation")
        reason = eligibility(post, call, self.clock())
        if reason:
            raise ValueError("cramer_" + reason)
        request = EntryRiskRequest(
            Strategy.CRAMER,
            quote.identity.coin,
            side,
            entry,
            proposed,
            quote.quantity_step,
            Decimal(1),
            notional,
            notional / LEVERAGE,
            Decimal(0),
            quote.maximum_leverage,
            LEVERAGE,
            liquidation,
            quote.ask - quote.bid,
            stop_price_step=tick,
            minimum_notional_usd=Decimal(10),
            require_full_requested_size=True,
        )
        return {
            "decision_id": "cramer:" + key,
            "strategy": Strategy.CRAMER,
            "request": request,
            "reconciliation": ReconciledAccount(
                "research-account", a.route.value, risk, True, True, True, True
            ),
            "reasons": (),
            "plan": {
                "entry_call": call.model_dump(mode="json"),
                "source_url": post.url,
                "stance": call.stance,
                "source_id": post.source_id,
                "expires_at": (call.statement_at + SIGNAL_TTL).isoformat(),
                "quote_at": quote.observed_at.isoformat(),
                "price_step": str(tick),
                "quantity_step": str(quote.quantity_step),
                "cost_estimates": "unavailable_informational",
                "maximum_hold_hours": 168,
            },
        }
