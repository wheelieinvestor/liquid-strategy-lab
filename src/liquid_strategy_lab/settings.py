"""Small explicit research settings; no account or credential fields."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

POLICY_NAMES = {
    "preserve-stop": "current-v5-missing-cache-hold",
    "legacy-exits": "legacy-production",
    "breakeven-1R": "breakeven-1R",
    "breakeven-1.25R": "breakeven-1.25R",
    "breakeven-1.5R": "breakeven-1.5R",
}
Policy = Literal[
    "preserve-stop", "legacy-exits", "breakeven-1R", "breakeven-1.25R", "breakeven-1.5R"
]


class BtcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    title: str = Field(default="BTC stop comparison", min_length=1, max_length=120)
    strategy: Literal["btc_momentum"] = "btc_momentum"
    dataset: str = "bundled:btc-demo"
    data_kind: Literal["synthetic", "proxy"] = "synthetic"
    data_description: str = Field(
        default="Authored synthetic BTC candles; not historical prices.", max_length=400
    )
    initial_cash: Decimal = Field(default=Decimal("1000"), ge=100, le=1000000)
    fee_per_side: Decimal = Field(default=Decimal("0.00095"), ge=0, le=Decimal("0.02"))
    spread_bps: Decimal = Field(default=Decimal("2"), ge=0, le=100)
    slippage_bps: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    policies: list[Policy] = Field(
        default=["preserve-stop", "breakeven-1R"], min_length=1, max_length=5
    )


class PortfolioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    title: str = Field(default="Four Liquid strategies, one simulated account", max_length=120)
    initial_cash: Decimal = Field(default=Decimal("1000"), ge=100, le=1000000)
    policy: Literal[
        "current-v5-exact", "legacy-production", "breakeven-1R", "breakeven-1.25R", "breakeven-1.5R"
    ] = "current-v5-exact"
    enabled: list[Literal["btc_momentum", "flow_show_mirror", "xyz100_gex", "inverse_cramer"]] = (
        Field(
            default=["btc_momentum", "flow_show_mirror", "xyz100_gex", "inverse_cramer"],
            min_length=1,
            max_length=4,
        )
    )
