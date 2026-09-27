"""Canonical captured Flow entries joined to native executable quotes and shared risk."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from liquid_autonomous_trader.controller import ReconciledAccount
from liquid_autonomous_trader.desk_store import DeskStore
from liquid_autonomous_trader.flow_epoch_eligibility import epoch_blocked_symbols
from liquid_autonomous_trader.flow_mandate import FLOW_MAX_PLANNED_LOSS_USD
from liquid_autonomous_trader.flow_market_preparation import prepare_flow_market
from liquid_autonomous_trader.flow_worker import _captured_from_row, _require_captured
from liquid_autonomous_trader.frozen.flow_mirror import (
    FLOW_MIRROR_V1_ALLOWED_SYMBOLS,
    FlowMirrorConfigV1,
    FlowMirrorEntryRiskLimitsV1,
    FlowMirrorFeeScheduleV1,
    FlowMirrorQuoteV1,
    FlowMirrorSleeveStateV1,
    assess_flow_mirror_entry,
    flow_mirror_risk_epoch,
)
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy
from liquid_autonomous_trader.native_perp_source import resolve_identity
from liquid_autonomous_trader.shared_account_risk import shared_risk_snapshot


class FlowLiveSource:
    def __init__(
        self,
        lifecycle,
        native,
        *,
        path,
        clock=lambda: datetime.now(UTC),
        capture=None,
        risk_projection=None,
    ):
        self.lifecycle, self.native, self.path, self.clock = lifecycle, native, Path(path), clock
        self.capture, self.risk_projection = capture, risk_projection
        self.db = lifecycle.risk_store.db
        self.config = FlowMirrorConfigV1(allowed_symbols=FLOW_MIRROR_V1_ALLOWED_SYMBOLS)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sleeve_source_cursors("
            "name TEXT PRIMARY KEY,seq INTEGER NOT NULL)"
        )
        self.db.execute("INSERT OR IGNORE INTO sleeve_source_cursors VALUES('flow',0)")

    def acknowledge(self, sequence):
        self.db.execute(
            "UPDATE sleeve_source_cursors SET seq=MAX(seq,?) WHERE name='flow'", (sequence,)
        )

    def __call__(self):
        now = self.clock()
        if self.capture is not None:
            captured, sequence = self.capture(now)
        else:
            sequence = None
            cursor = self.db.execute(
                "SELECT seq FROM sleeve_source_cursors WHERE name='flow'"
            ).fetchone()[0]
            with DeskStore(self.path, read_only=True) as store:
                store.verify()
                if store.get("paused") == "true" or store.get("revoked") == "true":
                    raise ValueError("flow_capture_halted")
                rows = store.db.execute(
                    "SELECT seq,body FROM events WHERE seq>? AND kind='source_delivery' AND "
                    "strategy='flow_show_mirror' ORDER BY seq LIMIT 100",
                    (cursor,),
                ).fetchall()
                captured = None
                for row in rows:
                    item = _captured_from_row(row)
                    _require_captured(store, item)
                    if (now - item.signal.delivered_at).total_seconds() > 300:
                        self.acknowledge(row["seq"])
                        continue
                    captured = item
                    sequence = row["seq"]
                    break
        if captured is None:
            return None
        signal = captured.signal
        identity_result = self.lifecycle.reconciler.client.call_read_tool(
            "analyze_market", {"symbol": signal.ticker}
        )
        if identity_result.is_error or not isinstance(identity_result.structured_content, dict):
            raise ValueError("flow_liquid_market_unavailable")
        identity = resolve_identity(
            signal.ticker, identity_result.structured_content, received_at=self.clock()
        )
        quote = self.native.quote(identity)
        prepared = prepare_flow_market(quote, side=signal.direction.value, now=self.clock())
        a, o, _ = self.lifecycle.reconciler.position(identity.coin, all_positions=True)
        projection = self.lifecycle.reconciler.accounting_projection
        risk = (self.risk_projection or shared_risk_snapshot)(
            projection,
            self.lifecycle.reconciler.before_account,
            a,
            o,
            journal=self.lifecycle.journal,
            executions=self.lifecycle.risk_store,
            now=self.clock(),
        )
        flow_markets = {
            r["symbol"]
            for r in self.lifecycle.risk_store.unresolved()
            if r["strategy"] == "flow_show_mirror"
        }
        active = {
            p.broker_symbol.removeprefix("xyz:")
            for p in a.positions
            if p.broker_symbol in flow_markets
        }
        epoch = flow_mirror_risk_epoch(self.clock())
        traded = epoch_blocked_symbols(self.db, self.lifecycle.journal, now=self.clock())
        sleeve = FlowMirrorSleeveStateV1(
            observed_at=a.received_at,
            risk_epoch=epoch,
            sleeve_nav_usd=a.equity_usd,
            deployed_margin_usd=sum(
                (p.margin_used_usd for p in a.positions if p.broker_symbol in flow_markets),
                Decimal(0),
            ),
            daily_negative_realized_usd=projection.conservative_loss,
            open_negative_pnl_usd=Decimal(0),
            active_symbols=active,
            traded_symbols_in_epoch=traded,
            account_halted=False,
        )
        decision = assess_flow_mirror_entry(
            config=self.config,
            signal=signal,
            quote=FlowMirrorQuoteV1(
                symbol=signal.ticker,
                observed_at=quote.observed_at,
                mark_price=quote.midpoint,
                bid_price=quote.bid,
                ask_price=quote.ask,
                maximum_leverage=quote.maximum_leverage,
                minimum_collateral_usd=None,
            ),
            fees=FlowMirrorFeeScheduleV1(
                observed_at=quote.received_at,
                market_entry_rate=prepared.fee_rate_ceiling_each_side,
                market_exit_rate=prepared.fee_rate_ceiling_each_side,
            ),
            sleeve=sleeve,
            now=self.clock(),
            cumulative_loss_limits_enabled=False,
            estimated_cost_controls_enabled=False,
            risk_limits=FlowMirrorEntryRiskLimitsV1(
                max_concurrent_positions=3,
                max_planned_loss_per_trade_usd=FLOW_MAX_PLANNED_LOSS_USD,
                daily_loss_cap_usd=Decimal(100),
            ),
        )
        reasons = tuple(decision.reasons) + prepared.reasons
        plan = {
            "delivery_id": signal.delivery_event_id,
            "entry_signal": signal.model_dump(mode="json"),
            "tp1_policy": "half_rounded_down_or_full_below_minimum",
            "entry_quantity": str(prepared.quantity),
            "entry_notional": str(prepared.submitted_notional),
            "quantity_step": str(quote.quantity_step),
            "price_step": str(
                max(
                    Decimal(1).scaleb(-6) / quote.quantity_step,
                    Decimal(1).scaleb(prepared.entry_reference.adjusted() - 4),
                )
            ),
            "funding_reserve": str(prepared.funding_reserve),
            "source_sequence": sequence,
            "fee_basis": prepared.fee_basis,
            "minimum_basis": "provider_enforced_unpublished",
        }
        if decision.decision.value != "enter" or prepared.reasons:
            return {
                "decision_id": "flow:" + signal.delivery_event_id,
                "strategy": Strategy.FLOW,
                "request": None,
                "reconciliation": None,
                "plan": plan,
                "reasons": reasons or ("flow_entry_skipped",),
            }
        if not 0 <= (self.clock() - quote.observed_at).total_seconds() <= 5:
            raise ValueError("flow_entry_quote_expired_during_account_read")
        entry = prepared.entry_reference
        stop = entry + entry * Decimal(".015") * (-1 if signal.direction.value == "long" else 1)
        request = EntryRiskRequest(
            Strategy.FLOW,
            identity.coin,
            signal.direction.value,
            entry,
            stop,
            quote.quantity_step,
            Decimal(1),
            prepared.submitted_notional,
            prepared.initial_margin,
            prepared.planned_roundtrip_fees
            + prepared.planned_roundtrip_slippage
            + prepared.funding_reserve,
            quote.maximum_leverage,
            Decimal(10),
            prepared.liquidation_distance,
            max(quote.ask - quote.bid, entry * Decimal(".001")),
            stop_price_step=Decimal(plan["price_step"]),
            minimum_notional_usd=Decimal(10),
            require_full_requested_size=True,
        )
        return {
            "decision_id": "flow:" + signal.delivery_event_id,
            "strategy": Strategy.FLOW,
            "request": request,
            "reconciliation": ReconciledAccount(
                "research-account", a.route.value, risk, True, True, True, True
            ),
            "plan": plan,
            "reasons": (),
        }
