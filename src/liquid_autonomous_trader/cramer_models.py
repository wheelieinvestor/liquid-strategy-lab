"""Versioned Cramer data, attribution and signal admission (no trading authority)."""

import hashlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from liquid_autonomous_trader.cramer_instruments import MACRO_COINS, macro_grounded

VERSION = "inverse-cramer-v2"
MAX_NOTIONAL = Decimal("500")
LEVERAGE = Decimal("10")
MAX_PRICE_LOSS = Decimal("15")
MAX_HOLD = timedelta(hours=168)
SIGNAL_TTL = timedelta(hours=2)
COOLDOWN = timedelta(hours=24)
OFFICIAL = frozenset({"jimcramer", "madmoneyoncnbc", "cnbc"})


class SourcePost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str = Field(pattern=r"^[0-9]{1,30}$")
    author_id: str = Field(pattern=r"^[0-9]{1,30}$")
    handle: Literal["jimcramer", "madmoneyoncnbc", "cnbc"]
    text: str = Field(min_length=1, max_length=12000)
    published_at: AwareDatetime
    discovered_at: AwareDatetime
    context: str = Field(default="", max_length=12000)
    context_complete: bool = True

    @property
    def url(self):
        return f"https://x.com/{self.handle}/status/{self.source_id}"


class ClaimInterpretation(BaseModel):
    """Grounded semantic metadata, without instrument or execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_start: int = Field(ge=0, le=12000, strict=True)
    evidence_end: int = Field(ge=1, le=12000, strict=True)
    speaker: Literal["cramer", "other", "unknown"]
    temporal_status: Literal["current", "historical", "conditional", "unknown"]
    horizon: Literal["near_term", "long_term", "mixed", "unspecified"]
    negation: Literal["none", "resolved", "uncertain"]
    ambiguity: Literal[
        "none",
        "instrument_mapping",
        "sarcasm",
        "missing_context",
        "conflicting_claims",
        "issuer",
        "timing",
        "meaning",
    ]
    abstention_reason: str = Field(max_length=300)


class InstrumentCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.]{0,19}$")
    issuer: str = Field(max_length=120)
    stance: Literal["bullish", "bearish", "neutral", "unclear"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)
    speaker_is_cramer: bool = Field(strict=True)
    explicit: bool = Field(strict=True)
    conditional: bool = Field(strict=True)
    historical: bool = Field(strict=True)
    evidence: str = Field(min_length=1, max_length=600)
    # Original statement time; publication of an old quote cannot reset its age.
    statement_at: AwareDatetime
    uncertainty: str = Field(max_length=300)
    # Old records with uncertainty retain their conservative interpretation.
    uncertainty_kind: Literal["none", "instrument_mapping", "meaning"] = "meaning"
    # Legacy persisted classifications remain readable. New provider requests
    # require this field; null means interpretation is unverified, not permission.
    interpretation: ClaimInterpretation | None = None

    @field_validator("evidence")
    @classmethod
    def meaningful_evidence(cls, value):
        if not value.strip():
            raise ValueError("cramer_empty_evidence")
        return value


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_id: str = Field(pattern=r"^[0-9]{1,30}$")
    calls: list[InstrumentCall] = Field(max_length=20)

    @model_validator(mode="after")
    def unique_instrument_calls(self):
        if len({call.ticker for call in self.calls}) != len(self.calls):
            raise ValueError("cramer_duplicate_instrument_calls")
        return self


def call_key(post, call):
    return hashlib.sha256(f"{VERSION}:{post.source_id}:{call.ticker}".encode()).hexdigest()


def eligibility(post, call, now):
    if now.tzinfo is None:
        return "invalid_clock"
    if not post.context_complete:
        return "missing_thread_context"
    if not (post.published_at <= post.discovered_at <= now):
        return "invalid_source_time"
    if not post.published_at >= call.statement_at:
        return "future_statement"
    if not timedelta(0) <= now - call.statement_at <= SIGNAL_TTL:
        return "source_expired"
    if call.evidence not in post.text:
        return "evidence_not_in_source"
    if detail := call.interpretation:
        if post.text[detail.evidence_start : detail.evidence_end] != call.evidence:
            return "evidence_span_mismatch"
        if detail.speaker != "cramer":
            return "missing_explicit_cramer_attribution"
        mapping_only = detail.ambiguity == "instrument_mapping" and macro_grounded(post, call)
        if (
            detail.temporal_status != "current"
            or detail.negation == "uncertain"
            or detail.horizon == "mixed"
            or detail.abstention_reason
            or (detail.ambiguity != "none" and not mapping_only)
        ):
            return "ambiguous_or_historical_statement"
    if not call.speaker_is_cramer or not call.explicit:
        return "missing_explicit_cramer_attribution"
    if call.ticker in MACRO_COINS and not macro_grounded(post, call):
        return "macro_instrument_not_grounded"
    resolved_alias = call.uncertainty_kind == "instrument_mapping" and macro_grounded(post, call)
    if call.conditional or call.historical or (call.uncertainty and not resolved_alias):
        return "ambiguous_or_historical_statement"
    if call.stance not in {"bullish", "bearish"} or call.confidence < 0.90:
        return "non_directional_or_low_confidence"
    return None


def inverse(call):
    if call.stance not in {"bullish", "bearish"}:
        raise ValueError("cramer_non_directional_call")
    return "short" if call.stance == "bullish" else "long"


def fingerprint(call):
    # Same statement carried by several official accounts gets one identity.
    evidence = re.sub(r"\W+", " ", call.evidence.casefold()).strip()
    return hashlib.sha256(f"{call.ticker}:{call.stance}:{evidence}".encode()).hexdigest()


def utcnow():
    return datetime.now(UTC)
