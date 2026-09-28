"""Small explicit research settings; no account or credential fields."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    title: str = Field(default="Test a strategy across five fictional markets", max_length=120)
    strategy: Literal["sma_trend", "custom"] = "sma_trend"
    strategy_file: str | None = None
    fast: int = Field(default=20, ge=1, le=199)
    slow: int = Field(default=50, ge=2, le=200)
    direction: Literal["long_short", "long_only"] = "long_short"
    initial_cash: Decimal = Field(default=Decimal("1000"), ge=1, le=1000000)
    position_notional: Decimal = Field(default=Decimal("200"), gt=0, le=1000000)
    seed: int = Field(default=7, ge=0, le=2147483647)
    bars: int = Field(default=512, ge=300, le=10000)

    @model_validator(mode="after")
    def consistent_rules(self):
        if self.fast >= self.slow:
            raise ValueError("fast_must_be_less_than_slow")
        if self.position_notional > self.initial_cash:
            raise ValueError("position_notional_cannot_exceed_initial_cash")
        if (self.strategy == "custom") != bool(self.strategy_file):
            raise ValueError("custom_strategy_requires_strategy_file_only")
        return self


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
