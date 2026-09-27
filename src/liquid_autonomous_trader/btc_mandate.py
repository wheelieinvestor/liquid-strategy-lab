"""Approved BTC sizing and per-trade loss limits."""

from decimal import Decimal

# User approved $50 on 2026-09-11; subsequent cost mandate excludes estimates.
BTC_MAX_PLANNED_LOSS_USD = Decimal("50")
BTC_TARGET_COLLATERAL_USD = Decimal("50")
BTC_TARGET_LEVERAGE = Decimal("40")
BTC_MAX_ORDER_NOTIONAL_USD = BTC_TARGET_COLLATERAL_USD * BTC_TARGET_LEVERAGE
OTHER_STRATEGY_MAX_ORDER_NOTIONAL_USD = Decimal("100")
