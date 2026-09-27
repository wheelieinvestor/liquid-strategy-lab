"""Transport-neutral Liquid quote, fee, and account join for captured Flow entries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from liquid_autonomous_trader.desk_inputs import FlowInput, ShadowInput
from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.frozen.flow_mirror import (
    FlowMirrorFeeScheduleV1,
    FlowMirrorQuoteV1,
    FlowMirrorSignalV1,
    FlowMirrorSleeveStateV1,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CapturedFlowDeliveryV1(StrictModel):
    schema_version: Literal["flow-captured-delivery-v1"] = "flow-captured-delivery-v1"
    source: Literal["flow_show_deliveries_v1"]
    source_contract: Literal["confirmed_directional_entry_only"]
    signal: FlowMirrorSignalV1
    captured_at: AwareDatetime
    capture_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LiquidPerpQuoteEvidenceV1(StrictModel):
    schema_version: Literal["liquid-perp-quote-v1"] = "liquid-perp-quote-v1"
    venue: Literal["liquid"]
    market_id: str = Field(min_length=1, max_length=128)
    instrument_mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: FlowMirrorQuoteV1
    received_at: AwareDatetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def causal(self) -> LiquidPerpQuoteEvidenceV1:
        if self.received_at < self.quote.observed_at:
            raise ValueError("quote_received_before_observed")
        return self


class LiquidFeeEvidenceV1(StrictModel):
    schema_version: Literal["liquid-fee-schedule-v1"] = "liquid-fee-schedule-v1"
    venue: Literal["liquid"]
    account_alias: str = Field(min_length=1, max_length=128)
    fee_tier_id: str = Field(min_length=1, max_length=128)
    fees: FlowMirrorFeeScheduleV1
    received_at: AwareDatetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def causal(self) -> LiquidFeeEvidenceV1:
        if self.received_at < self.fees.observed_at:
            raise ValueError("fees_received_before_observed")
        return self


class LiquidAccountSleeveEvidenceV1(StrictModel):
    schema_version: Literal["liquid-flow-account-sleeve-v1"] = "liquid-flow-account-sleeve-v1"
    venue: Literal["liquid"]
    account_alias: str = Field(min_length=1, max_length=128)
    route_id: str = Field(min_length=1, max_length=128)
    sleeve_id: Literal["flow_show_mirror"]
    sleeve: FlowMirrorSleeveStateV1
    received_at: AwareDatetime
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def causal(self) -> LiquidAccountSleeveEvidenceV1:
        if self.received_at < self.sleeve.observed_at:
            raise ValueError("account_received_before_observed")
        return self


class FlowJoinEvidenceV1(StrictModel):
    schema_version: Literal["flow-liquid-entry-join-v1"] = "flow-liquid-entry-join-v1"
    quote: LiquidPerpQuoteEvidenceV1
    fees: LiquidFeeEvidenceV1
    account: LiquidAccountSleeveEvidenceV1

    @model_validator(mode="after")
    def one_account(self) -> FlowJoinEvidenceV1:
        if self.fees.account_alias != self.account.account_alias:
            raise ValueError("flow_account_alias_mismatch")
        return self


def prepare_flow_input(
    captured: CapturedFlowDeliveryV1,
    joined: FlowJoinEvidenceV1,
    now: datetime,
) -> tuple[ShadowInput, dict]:
    """Prepare the frozen entry-only strategy input without granting source authority."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("flow_join_time_must_be_aware")
    if captured.captured_at < captured.signal.delivered_at:
        raise ValueError("capture_before_delivery")
    if captured.captured_at > now:
        raise ValueError("future_flow_capture")
    if any(
        received_at > now
        for received_at in (
            joined.quote.received_at,
            joined.fees.received_at,
            joined.account.received_at,
        )
    ):
        raise ValueError("future_flow_join_receipt")
    if joined.quote.quote.symbol != captured.signal.ticker:
        raise ValueError("perp_quote_symbol_mismatch")
    input_data = FlowInput(
        signal=captured.signal,
        quote=joined.quote.quote,
        fees=joined.fees.fees,
        sleeve=joined.account.sleeve,
    )
    provenance = {
        "schema_version": "flow-input-provenance-v1",
        "delivery_event_id": captured.signal.delivery_event_id,
        "source_view": captured.source,
        "source_contract": captured.source_contract,
        "capture_receipt_sha256": captured.capture_receipt_sha256,
        "venue": joined.quote.venue,
        "market_id": joined.quote.market_id,
        "instrument_mapping_sha256": joined.quote.instrument_mapping_sha256,
        "account_alias": joined.account.account_alias,
        "route_id": joined.account.route_id,
        "fee_tier_id": joined.fees.fee_tier_id,
        "quote_payload_sha256": joined.quote.payload_sha256,
        "fee_payload_sha256": joined.fees.payload_sha256,
        "account_payload_sha256": joined.account.payload_sha256,
        "joined_evidence_sha256": digest(joined.model_dump(mode="json")),
        "source_authenticated": False,
        "execution_authorized": False,
    }
    return (
        ShadowInput(
            schema_version="shadow-input-v1",
            evidence_type="supplied_shadow",
            strategy="flow_show_mirror",
            source_id="flow-entry:" + captured.signal.delivery_event_id,
            observed_at=max(
                joined.quote.quote.observed_at,
                joined.fees.fees.observed_at,
                joined.account.sleeve.observed_at,
            ),
            candle_closed=True,
            observation=input_data.model_dump(mode="json"),
        ),
        provenance,
    )
