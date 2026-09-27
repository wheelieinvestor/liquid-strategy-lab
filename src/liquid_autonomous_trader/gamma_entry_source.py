"""Native XYZ entry requests at the user-approved $100 margin / $3,000 notional."""

from decimal import ROUND_FLOOR, Decimal

from liquid_autonomous_trader.controller import ReconciledAccount
from liquid_autonomous_trader.frozen.models import SignalAction
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy
from liquid_autonomous_trader.shared_account_risk import shared_risk_snapshot
from liquid_autonomous_trader.xyz_mandate import XYZ_MAX_ORDER_NOTIONAL_USD, XYZ_TARGET_LEVERAGE


class GammaEntrySource:
    def __init__(self, source, lifecycle, *, clock, risk_projection=None):
        self.source, self.lifecycle, self.clock = source, lifecycle, clock
        self.risk_projection = risk_projection

    def __call__(self):
        bundle = self.source()
        if bundle is None:
            return None
        decision_id, signal, observation, quote, provenance = bundle
        plan = {
            "provenance": provenance,
            "entry_observation": observation.model_dump(mode="json"),
            "fee_basis": "published_undiscounted_ceiling_not_account_fee_tier",
        }
        if signal.action != SignalAction.ENTER:
            return {
                "decision_id": decision_id,
                "strategy": Strategy.XYZ,
                "request": None,
                "reconciliation": None,
                "plan": plan,
                "reasons": signal.reason_codes,
            }
        if quote.maximum_leverage < XYZ_TARGET_LEVERAGE:
            raise ValueError("gamma_required_leverage_unavailable")
        a, o, _ = self.lifecycle.reconciler.position("xyz:XYZ100", all_positions=True)
        risk = (self.risk_projection or shared_risk_snapshot)(
            self.lifecycle.reconciler.accounting_projection,
            self.lifecycle.reconciler.before_account,
            a,
            o,
            journal=self.lifecycle.journal,
            executions=self.lifecycle.risk_store,
            now=self.clock(),
        )
        if not 0 <= (self.clock() - quote.observed_at).total_seconds() <= 5:
            raise ValueError("gamma_entry_quote_expired_during_account_read")
        entry = quote.ask if signal.direction.value == "long" else quote.bid
        # Fixed margin/notional mandate, rounded down to whole native quantity lots.
        quantity = (XYZ_MAX_ORDER_NOTIONAL_USD / entry / quote.quantity_step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * quote.quantity_step
        notional = quantity * entry
        if notional < 10:
            raise ValueError("gamma_venue_minimum_notional_not_met")
        if min(quote.bid_notional, quote.ask_notional) < XYZ_MAX_ORDER_NOTIONAL_USD * 5:
            raise ValueError("gamma_insufficient_market_depth")
        leverage = XYZ_TARGET_LEVERAGE
        scale = quote.deployer_fee_scale
        fee = Decimal(".00045") * (1 + scale if scale < 1 else 2 * scale) + Decimal(".0005")
        slip = max(quote.ask - quote.bid, entry * Decimal(".001"))
        costs = notional * (fee * 2 + 2 * slip / entry) + Decimal(1)
        # A planning reserve, not a claimed paid funding charge.
        maintenance = 1 / (2 * quote.maximum_leverage)
        liquidation = entry * (1 / leverage - maintenance) / (1 + maintenance)
        tick = max(
            Decimal(1).scaleb(-6) / quote.quantity_step,
            Decimal(1).scaleb(signal.bracket.stop_price.adjusted() - 4),
        )
        request = EntryRiskRequest(
            Strategy.XYZ,
            "xyz:XYZ100",
            signal.direction.value,
            entry,
            signal.bracket.stop_price,
            quote.quantity_step,
            Decimal(1),
            notional,
            notional / leverage,
            costs,
            quote.maximum_leverage,
            leverage,
            liquidation,
            slip,
            stop_price_step=tick,
            minimum_notional_usd=Decimal(10),
            require_full_requested_size=True,
        )
        plan.update(
            target=str(signal.bracket.target_price),
            price_step=str(tick),
            quantity_step=str(quote.quantity_step),
            funding_reserve="1",
        )
        return {
            "decision_id": decision_id,
            "strategy": Strategy.XYZ,
            "request": request,
            "reconciliation": ReconciledAccount(
                "research-account", a.route.value, risk, True, True, True, True
            ),
            "plan": plan,
            "reasons": (),
        }

    def management_atr(self):
        """Native closed-candle ATR for exits, independent of QQQ availability/session."""
        from liquid_autonomous_trader.native_perp_source import NativeCandle

        started = self.clock()
        end = int(started.timestamp() * 1000)
        raw = self.source.native.fetch(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": "xyz:XYZ100",
                    "interval": "15m",
                    "startTime": end - 86400000,
                    "endTime": end,
                },
            }
        )
        if not isinstance(raw, list) or not 15 <= len(raw) <= 100:
            raise ValueError("gamma_exit_atr_history_missing")
        rows = [NativeCandle.model_validate(row) for row in raw]
        if any(
            c.s != "xyz:XYZ100"
            or c.t % 900000
            or c.T != c.t + 899999
            or not c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h
            or c.t > end
            for c in rows
        ):
            raise ValueError("gamma_exit_candle_invalid")
        if any(b.t - a.t != 900000 for a, b in zip(rows, rows[1:])):
            raise ValueError("gamma_exit_candle_gap")
        closed = [c for c in rows if c.T <= end - 2000]
        if len(closed) < 15 or closed[-1].T + 1 != int((started.timestamp() - 2) // 900) * 900000:
            raise ValueError("gamma_exit_candle_stale")
        if not 0 <= (self.clock() - started).total_seconds() <= 10:
            raise ValueError("gamma_exit_candle_fetch_stale")
        return (
            sum(
                max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c))
                for a, b in zip(closed[-15:], closed[-14:])
            )
            / 14
        )
