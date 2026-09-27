"""Strict supplied-input evaluation; source assertions never grant trading authority."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool

from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.frozen.flow_mirror import (
    FLOW_MIRROR_V1_ALLOWED_SYMBOLS,
    FlowMirrorConfigV1,
    FlowMirrorEntryRiskLimitsV1,
    FlowMirrorFeeScheduleV1,
    FlowMirrorQuoteV1,
    FlowMirrorSignalV1,
    FlowMirrorSleeveStateV1,
    assess_flow_mirror_entry,
)
from liquid_autonomous_trader.frozen.strategies.btc_momentum import (
    BtcMomentumConfigV1,
    BtcMomentumEngineV1,
    BtcMomentumObservationV1,
)
from liquid_autonomous_trader.frozen.strategies.xyz100_gex import (
    XYZ100GexConfigV1,
    XYZ100GexEngineV1,
    XYZ100GexObservationV1,
)

STRATEGIES = ("btc_momentum", "xyz100_gex", "flow_show_mirror")


class ShadowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["shadow-input-v1"]
    evidence_type: Literal["fixture", "supplied_shadow"]
    strategy: Literal["btc_momentum", "xyz100_gex", "flow_show_mirror"]
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    observed_at: AwareDatetime
    candle_closed: StrictBool
    observation: dict


class FlowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    signal: FlowMirrorSignalV1
    quote: FlowMirrorQuoteV1
    fees: FlowMirrorFeeScheduleV1
    sleeve: FlowMirrorSleeveStateV1


def fresh(timestamp: datetime, now: datetime, seconds: int) -> None:
    if timestamp.tzinfo is None or now.tzinfo is None:
        raise ValueError("timezone_required")
    age = (now - timestamp).total_seconds()
    if not 0 <= age <= seconds:
        raise ValueError("stale_or_future_input")


def evaluate(item: ShadowInput, now: datetime) -> dict:
    context: dict = {}
    fresh(item.observed_at, now, 10)
    if not item.candle_closed:
        raise ValueError("closed_candle_required")
    if item.strategy == "btc_momentum":
        observation = BtcMomentumObservationV1.model_validate(item.observation)
        fresh(observation.market.observed_at, now, 10)
        result = BtcMomentumEngineV1(BtcMomentumConfigV1()).evaluate(observation)
    elif item.strategy == "xyz100_gex":
        observation = XYZ100GexObservationV1.model_validate(item.observation)
        fresh(observation.market.observed_at, now, 10)
        fresh(observation.gex.observed_at, now, 300)
        gex = observation.gex
        if not (gex.downside_target < gex.support_level < gex.resistance_level < gex.upside_target):
            raise ValueError("invalid_gex_geometry")
        # Shadow pilot cap only. This neither configures broker leverage nor grants approval.
        result = XYZ100GexEngineV1(XYZ100GexConfigV1(max_leverage=Decimal("1"))).evaluate(
            observation
        )
        context = {
            "gex_context": observation.gex.model_dump(mode="json"),
            "operational_shadow_leverage_cap": "1",
        }
    else:
        observation = FlowInput.model_validate(item.observation)
        result = assess_flow_mirror_entry(
            config=FlowMirrorConfigV1(allowed_symbols=FLOW_MIRROR_V1_ALLOWED_SYMBOLS),
            signal=observation.signal,
            quote=observation.quote,
            fees=observation.fees,
            sleeve=observation.sleeve,
            now=now,
            risk_limits=FlowMirrorEntryRiskLimitsV1(
                max_concurrent_positions=3,
                max_planned_loss_per_trade_usd=Decimal("10"),
                daily_loss_cap_usd=Decimal("25"),
            ),
        )
    return {
        **context,
        "strategy_result": result.model_dump(mode="json"),
        "input_hash": digest(item.model_dump(mode="json")),
        "evidence_type": item.evidence_type,
        "source_verified": False,
        "closed_candle_source_verified": False,
        "risk_admitted": False,
        "reason_codes": [
            "execution_disabled",
            "supplied_inputs_not_source_attestation",
            "account_risk_not_evaluated",
        ],
    }
