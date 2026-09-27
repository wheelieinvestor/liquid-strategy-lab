"""BTC-only sizing from live venue metadata and the local account ledger.

Liquid owns all account/execution calls. The public Hyperliquid meta read is
market data, not a wallet/signer alternative. See docs/btc-production-runtime.md.
"""

from datetime import UTC, datetime
from decimal import Decimal

from liquid_autonomous_trader.btc_mandate import (
    BTC_MAX_ORDER_NOTIONAL_USD,
    BTC_TARGET_COLLATERAL_USD,
    BTC_TARGET_LEVERAGE,
)
from liquid_autonomous_trader.btc_source import _public_info
from liquid_autonomous_trader.controller import ReconciledAccount
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy


class BtcRiskProvider:
    def __init__(
        self, reconciler, *, fetch=_public_info, clock=lambda: datetime.now(UTC), risk_snapshot=None
    ):
        self.reconciler, self.fetch, self.clock = reconciler, fetch, clock
        self.risk_snapshot = risk_snapshot
        self.metadata = None
        self.metadata_at = None

    def refresh(self):
        meta = self.fetch({"type": "meta"})
        btc = [asset for asset in meta["universe"] if asset.get("name") == "BTC"]
        if len(btc) != 1 or btc[0].get("isDelisted"):
            raise ValueError("btc_venue_identity_invalid")
        btc = btc[0]
        decimals, leverage = btc["szDecimals"], btc["maxLeverage"]
        if type(decimals) is not int or not 0 <= decimals <= 6:
            raise ValueError("btc_venue_precision_invalid")
        if type(leverage) is not int or not 1 <= leverage <= 40:
            raise ValueError("btc_venue_leverage_invalid")
        tables = dict(meta["marginTables"])
        tiers = tables[btc["marginTableId"]]["marginTiers"]
        if (
            not tiers
            or Decimal(str(tiers[0]["lowerBound"])) != 0
            or tiers[0]["maxLeverage"] != leverage
            or (
                len(tiers) > 1
                and Decimal(str(tiers[1]["lowerBound"])) <= BTC_MAX_ORDER_NOTIONAL_USD
            )
        ):
            raise ValueError("btc_first_margin_tier_invalid")
        self.metadata = (Decimal(1).scaleb(-decimals), Decimal(leverage))
        self.metadata_at = self.clock()
        return self.metadata

    def __call__(self, observation, signal, account, orders, evidence):
        if (
            self.metadata_at is None
            or not 0 <= (self.clock() - self.metadata_at).total_seconds() <= 300
        ):
            raise ValueError("btc_venue_metadata_refresh_required")
        projection = self.reconciler.accounting_projection
        if projection is None:
            raise ValueError("local_accounting_required")
        risk = (
            self.risk_snapshot(account, orders)
            if self.risk_snapshot is not None
            else projection.flat_risk_snapshot(account, orders)
        )
        step, maximum = self.metadata
        entry = observation.market.price
        # Integer prices are explicitly valid at every BTC price magnitude.
        notional = BTC_MAX_ORDER_NOTIONAL_USD
        # Planning estimate, not a guaranteed slippage cap: two spread widths or
        # 1/4 current ATR, plus the published highest base taker round-trip rate.
        slip = max(observation.market.spread_price * 2, observation.market.atr_15m / 4)
        # This informational estimate covers known components only. Missing
        # funding remains null in the persisted entry observation, never a
        # fabricated zero rate; costs cannot constrain price-risk admission.
        known_funding_cost = (
            abs(observation.funding_rate) * 2
            if observation.funding_rate is not None
            else Decimal(0)
        )
        costs = notional * (Decimal("0.0019") + 2 * slip / entry + known_funding_cost)
        # Fixed user target: $50 initial margin at 40x, not automatic leverage.
        # Keep native quantity-rounding headroom; do not spend $50 times 40 twice.
        leverage = BTC_TARGET_LEVERAGE
        if leverage > min(maximum, signal.leverage):
            raise ValueError("btc_requested_leverage_unavailable")
        maintenance = 1 / (2 * maximum)
        # Worst (short-side) isolated formula. For a clean, funded cross account,
        # available equity is greater than this reserved isolated margin model.
        distance = entry * (1 / leverage - maintenance) / (1 + maintenance)
        # Reserve only quantity-rounding headroom; do not raise submitted USD.
        collateral = BTC_TARGET_COLLATERAL_USD + 2 * step * entry / leverage
        request = EntryRiskRequest(
            Strategy.BTC,
            "BTC",
            signal.direction.value,
            entry,
            signal.bracket.stop_price,
            step,
            Decimal(1),
            notional,
            collateral,
            costs,
            maximum,
            leverage,
            distance,
            slip,
            stop_price_step=Decimal(1),
            minimum_notional_usd=Decimal("10"),
            require_full_requested_size=True,
        )
        return (
            request,
            ReconciledAccount(
                "research-account",
                account.route.value,
                risk,
                True,
                orders.working_snapshot_complete,
                True,
                True,
            ),
            Decimal(1),
        )
