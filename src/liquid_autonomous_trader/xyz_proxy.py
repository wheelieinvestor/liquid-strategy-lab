"""QQQ-derived XYZ100 shadow inputs. No broker, credential, or order capability."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool

from liquid_autonomous_trader.desk_inputs import ShadowInput, evaluate, fresh
from liquid_autonomous_trader.desk_store import DeskStore, digest
from liquid_autonomous_trader.frozen.itmatrix_xyz100 import ITMatrixGexRecordV1
from liquid_autonomous_trader.frozen.models import MarketContextV1, SignalDirection
from liquid_autonomous_trader.frozen.strategies.xyz100_gex import (
    GexRegimeV1,
    XYZ100GexSnapshotV1,
)


def resolve_xyz_symbol(symbol: str) -> dict[str, str]:
    if symbol.strip().upper() not in {"XYZ100", "NASDAQ100", "XYZ:XYZ100"}:
        raise ValueError("unknown_xyz_alias")
    return {"strategy": "XYZ100", "liquid_lookup": "NASDAQ100", "market_id": "xyz:XYZ100"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ReferencePair(StrictModel):
    source_symbol: Literal["QQQ"]
    market_id: Literal["xyz:XYZ100"]
    qqq_price: Decimal = Field(gt=0)
    xyz_price: Decimal = Field(gt=0)
    qqq_observed_at: AwareDatetime
    xyz_observed_at: AwareDatetime
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CashSession(StrictModel):
    """Explicit calendar interval: do not infer holidays from weekday alone."""

    opens_at: AwareDatetime
    closes_at: AwareDatetime
    calendar_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProxyObservation(StrictModel):
    market: MarketContextV1
    prior_close: Decimal = Field(gt=0)
    latest_close: Decimal = Field(gt=0)
    trend_1h: SignalDirection


class ProxyInput(StrictModel):
    schema_version: Literal["xyz-qqq-proxy-v1"]
    record: ITMatrixGexRecordV1
    reference: ReferencePair
    session: CashSession
    current_qqq_price: Decimal = Field(gt=0)
    current_qqq_observed_at: AwareDatetime
    observation: ProxyObservation
    candle_closed: StrictBool


def prepare_shadow(payload: dict, now: datetime) -> ShadowInput:
    """Replaces supplied GEX geometry; supplied evidence never proves live connectivity."""
    item = ProxyInput.model_validate(payload)
    record, ref, session, obs = item.record, item.reference, item.session, item.observation
    fresh(record.source_timestamp, now, 300)
    fresh(ref.qqq_observed_at, now, 300)
    fresh(ref.xyz_observed_at, now, 300)
    fresh(item.current_qqq_observed_at, now, 10)
    fresh(obs.market.observed_at, now, 10)
    if not item.candle_closed:
        raise ValueError("closed_candle_required")
    if record.symbol != "QQQ" or not record.complete or record.revision_ambiguous:
        raise ValueError("complete_unambiguous_qqq_required")
    resolve_xyz_symbol(obs.market.symbol)
    times = [record.source_timestamp, ref.qqq_observed_at, ref.xyz_observed_at]
    if (max(times) - min(times)).total_seconds() > 2:
        raise ValueError("reference_time_skew")
    if abs((obs.market.observed_at - item.current_qqq_observed_at).total_seconds()) > 2:
        raise ValueError("current_quote_time_skew")
    eastern = ZoneInfo("America/New_York")
    opening, closing = session.opens_at.astimezone(eastern), session.closes_at.astimezone(eastern)
    if (
        opening.date() != closing.date()
        or opening.weekday() >= 5
        or (opening.hour, opening.minute, opening.second, opening.microsecond) != (9, 30, 0, 0)
        or not opening < closing
        or (closing.hour, closing.minute, closing.second) > (16, 0, 0)
        or not session.opens_at <= min(times) <= now < session.closes_at
    ):
        raise ValueError("verified_cash_session_required")
    ratio = ref.xyz_price / ref.qqq_price
    current_ratio = obs.market.price / item.current_qqq_price
    # Conservative preparation threshold, not an empirically calibrated trading parameter.
    if abs(current_ratio / ratio - 1) * 10000 > Decimal("25"):
        raise ValueError("proxy_basis_drift")
    names = (
        "downside_target",
        "support_level",
        "zero_gamma_level",
        "resistance_level",
        "upside_target",
    )
    levels = {
        name: None if getattr(record, name) is None else getattr(record, name) * ratio
        for name in names
    }
    if (
        not levels["downside_target"]
        < levels["support_level"]
        < levels["resistance_level"]
        < levels["upside_target"]
    ):
        raise ValueError("invalid_gex_geometry")
    provenance = digest(item.model_dump(mode="json"))
    snapshot = XYZ100GexSnapshotV1(
        source_symbol="XYZ100",
        source_underlying="QQQ",
        mapping_method="synchronized_ratio_proxy",
        observed_at=record.source_timestamp,
        provenance_sha256=provenance,
        complete=True,
        identity_verified=True,
        revision_ambiguous=False,
        regime=GexRegimeV1.POSITIVE
        if record.net_gex_dollars > 0
        else (GexRegimeV1.NEGATIVE if record.net_gex_dollars < 0 else GexRegimeV1.TRANSITION),
        **levels,
    )
    observation = obs.model_dump(mode="json")
    observation["gex"] = snapshot.model_dump(mode="json")
    observation["market"]["symbol"] = "XYZ100"
    return ShadowInput(
        schema_version="shadow-input-v1",
        evidence_type="supplied_shadow",
        strategy="xyz100_gex",
        source_id="qqq-proxy:" + provenance,
        observed_at=obs.market.observed_at,
        candle_closed=True,
        observation=observation,
    )


def ingest_proxy(store: DeskStore, payload: dict, now: datetime) -> dict:
    """Replay a recorded decision before age checks; never execute or re-evaluate it."""
    from liquid_autonomous_trader.desk_runtime import envelope

    store.verify()
    item = ProxyInput.model_validate(payload)
    if store.get("paused") == "true" or store.get("revoked") == "true":
        return envelope(status="HALTED_NO_ENTRY", reason_codes=["operator_halt"])
    fingerprint = digest(item.model_dump(mode="json"))
    event_id = "proxy-input:xyz100_gex:" + fingerprint
    previous = store.previous(event_id, fingerprint)
    if previous is not None:
        return {**previous, "duplicate": True}
    shadow = prepare_shadow(payload, now)
    body = envelope(
        status="SHADOW_EVALUATED",
        strategy="xyz100_gex",
        **evaluate(shadow, now),
        proxy_provenance={
            "underlying": "QQQ",
            "mapping_method": "synchronized_ratio_proxy",
            "reference": item.reference.model_dump(mode="json"),
            "session": item.session.model_dump(mode="json"),
            "source_timestamp": item.record.source_timestamp.isoformat(),
            "source_response_sha256": item.record.raw_response_sha256,
            "input_fingerprint": fingerprint,
            "source_and_calendar_authenticated": False,
        },
    )
    result, created = store.record(
        event_id, "shadow_decision", "xyz100_gex", fingerprint, body, require_active=True
    )
    return {**result, "duplicate": not created}
