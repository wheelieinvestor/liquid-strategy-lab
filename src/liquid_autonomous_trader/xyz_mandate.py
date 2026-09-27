"""XYZ sizing explicitly revised by the user on 2026-09-11."""

from decimal import Decimal

XYZ_TARGET_COLLATERAL_USD = Decimal("100")
XYZ_TARGET_LEVERAGE = Decimal("30")
XYZ_MAX_ORDER_NOTIONAL_USD = XYZ_TARGET_COLLATERAL_USD * XYZ_TARGET_LEVERAGE
XYZ_MAX_PRICE_STOP_LOSS_USD = Decimal("15")
