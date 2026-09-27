"""Flow sizing and risk revision explicitly approved on 2026-09-10."""

from decimal import Decimal

FLOW_TARGET_COLLATERAL_USD = Decimal("50")
FLOW_TARGET_LEVERAGE = Decimal("10")
FLOW_MAX_ORDER_NOTIONAL_USD = FLOW_TARGET_COLLATERAL_USD * FLOW_TARGET_LEVERAGE
FLOW_MAX_PLANNED_LOSS_USD = Decimal("15")
FLOW_FUNDING_RESERVE_USD = Decimal("3")
