"""Copied market-data schemas; all market reads are injected in this distribution."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

BAR_MS = 900_000


def _public_info(*args, **kwargs):
    raise RuntimeError("network_source_not_available_use_imported_events")


class Candle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    t: StrictInt = Field(ge=0)
    T: StrictInt = Field(ge=0)
    s: Literal["BTC"]
    i: Literal["15m"]
    o: Decimal = Field(gt=0)
    h: Decimal = Field(gt=0)
    l: Decimal = Field(gt=0)  # noqa: E741 - exact provider low-price field
    c: Decimal = Field(gt=0)
    v: Decimal = Field(ge=0)
    n: StrictInt = Field(ge=0)


class Level(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    px: Decimal = Field(gt=0)
    sz: Decimal = Field(gt=0)
    n: StrictInt = Field(gt=0)


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    coin: Literal["BTC"]
    time: StrictInt = Field(ge=0)
    levels: tuple[list[Level], list[Level]]


class FundingRate(BaseModel):
    """A realized Hyperliquid BTC funding rate, expressed as a decimal fraction per hour."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    coin: Literal["BTC"]
    fundingRate: Decimal
    premium: Decimal
    time: StrictInt = Field(ge=0)

    @property
    def rate_fraction_per_hour(self) -> Decimal:
        return self.fundingRate
