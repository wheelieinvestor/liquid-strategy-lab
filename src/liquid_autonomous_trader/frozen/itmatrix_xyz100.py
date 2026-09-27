"""Strict normalization boundary for future IT Matrix XYZ100 GEX snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from liquid_autonomous_trader.frozen.strategies.xyz100_gex import GexRegimeV1, XYZ100GexSnapshotV1


class XYZ100GexMappingV1(BaseModel):
    """Explicit economic-underlying identity contract; no proxy inference is permitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    liquid_symbol: str = "XYZ100"
    itmatrix_symbol: str = Field(min_length=1, max_length=32)
    mapping_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = False


class ITMatrixGexRecordV1(BaseModel):
    """Minimal provider record required to build a tradable GEX map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    source_timestamp: datetime
    complete: bool
    revision_ambiguous: bool = False
    net_gex_dollars: Decimal
    zero_gamma_level: Decimal | None = Field(default=None, gt=0)
    support_level: Decimal = Field(gt=0)
    resistance_level: Decimal = Field(gt=0)
    downside_target: Decimal = Field(gt=0)
    upside_target: Decimal = Field(gt=0)
    raw_response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class XYZ100GexNormalizerV1:
    """Convert an already-fetched IT Matrix record into a fail-closed strategy snapshot."""

    def normalize(
        self, record: ITMatrixGexRecordV1, mapping: XYZ100GexMappingV1
    ) -> XYZ100GexSnapshotV1:
        if not mapping.approved:
            raise ValueError("XYZ100 IT Matrix mapping is not approved")
        if record.symbol.upper() == "QQQ":
            raise ValueError("QQQ requires explicit proxy conversion, not identity relabeling")
        if mapping.liquid_symbol != "XYZ100":
            raise ValueError("only the explicit XYZ100 Liquid mapping is supported")
        if record.symbol.upper() != mapping.itmatrix_symbol.upper():
            raise ValueError("IT Matrix record symbol does not match the approved mapping")
        if record.support_level >= record.resistance_level:
            raise ValueError("GEX support must be below resistance")
        if record.downside_target >= record.support_level:
            raise ValueError("GEX downside target must be below support")
        if record.upside_target <= record.resistance_level:
            raise ValueError("GEX upside target must be above resistance")
        material: dict[str, Any] = {
            "mapping_evidence_sha256": mapping.mapping_evidence_sha256,
            "raw_response_sha256": record.raw_response_sha256,
            "symbol": record.symbol.upper(),
            "source_timestamp": record.source_timestamp.isoformat(),
            "net_gex_dollars": str(record.net_gex_dollars),
        }
        provenance = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return XYZ100GexSnapshotV1(
            source_symbol="XYZ100",
            observed_at=record.source_timestamp,
            provenance_sha256=provenance,
            complete=record.complete,
            identity_verified=True,
            revision_ambiguous=record.revision_ambiguous,
            regime=GexRegimeV1.POSITIVE if record.net_gex_dollars >= 0 else GexRegimeV1.NEGATIVE,
            support_level=record.support_level,
            resistance_level=record.resistance_level,
            zero_gamma_level=record.zero_gamma_level,
            downside_target=record.downside_target,
            upside_target=record.upside_target,
        )
