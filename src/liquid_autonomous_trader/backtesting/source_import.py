"""Normalize sanitized existing source exports, without any provider or DB access."""

from __future__ import annotations

from datetime import datetime

from liquid_autonomous_trader.backtesting.events import Event, canonical, digest, require_sanitized
from liquid_autonomous_trader.cramer_models import Classification, SourcePost
from liquid_autonomous_trader.flow_inputs import CapturedFlowDeliveryV1


def micros(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("source_timestamp_timezone_required")
    return int(stamp.timestamp() * 1_000_000)


def normalize_sources(records, archives):
    require_sanitized(records)
    require_sanitized(archives)
    if (
        records["schema"] != "liquid-sanitized-evidence-v1"
        or archives["schema"] != "liquid-source-archive-export-v1"
    ):
        raise ValueError("sanitized_source_schema_required")
    if (
        records["production_writes"]
        or records["model_inference"]
        or archives["database_writes"]
        or archives["source_calls"]
        or archives["inference_calls"]
    ):
        raise ValueError("readonly_existing_source_export_required")
    events, gaps = [], []

    def add(
        source,
        kind,
        instrument,
        event_us,
        available_us,
        retrieved_us,
        sequence,
        payload,
        quality="observed",
        lineage=(),
    ):
        events.append(
            Event(
                source=source,
                instrument=instrument,
                venue="source",
                kind=kind,
                event_us=event_us,
                published_us=event_us,
                available_us=available_us,
                retrieved_us=retrieved_us,
                sequence=sequence,
                revision=0,
                units="source_contract",
                quality=quality,
                reference="sanitized-existing-source-archive",
                payload_json=canonical(payload),
                lineage=lineage,
            )
        )

    for row in records.get("flow_deliveries", []):
        body = row["body"]
        if body.get("status") != "SOURCE_CAPTURED_NOT_EVALUATED":
            raise ValueError("confirmed_flow_delivery_required")
        captured = CapturedFlowDeliveryV1(
            source=body["source"],
            source_contract=body["source_contract"],
            signal=body["signal"],
            captured_at=body["observed_at"],
            capture_receipt_sha256=digest(body),
        )
        add(
            "flow-delivery-archive-v1",
            "flow_delivery",
            captured.signal.ticker,
            micros(captured.signal.delivered_at.isoformat()),
            micros(body["observed_at"]),
            micros(records["observed_at"]),
            row["seq"],
            captured.model_dump(mode="json"),
            lineage=(row["event_hash"],),
        )
    for row in archives.get("cramer", []):
        post = SourcePost.model_validate(row["body"]["post"])
        classified = Classification.model_validate(row["body"]["classification"])
        if classified.source_id != post.source_id:
            raise ValueError("classification_post_identity_mismatch")
        if not row.get("created_at"):
            gaps.append("cramer_classification_arrival_missing")
            continue
        available = micros(row["created_at"])
        if available < micros(post.discovered_at.isoformat()):
            raise ValueError("classification_precedes_source_discovery")
        add(
            "cramer-classification-archive-v1",
            "cramer_classification",
            "CRAMER",
            micros(post.published_at.isoformat()),
            available,
            micros(archives["observed_at"]),
            row["seq"],
            {key: row[key] for key in ("body", "model", "prompt_sha")},
            quality="partial",
        )
    for row in archives.get("gamma", []):
        result = row["result"]
        if result.get("symbol") != "QQQ" or row["kind"] not in {"matrix", "spot"}:
            raise ValueError("gamma_archive_instrument_or_lane")
        times = (
            [micros(e["updatedAt"]) for e in result["expirations"]]
            if row["kind"] == "matrix"
            else [micros(result["updatedAt"])]
        )
        if not times:
            gaps.append("gamma_empty_expiration_evidence")
            continue
        add(
            "qqq-gex-archive-v1",
            "gamma_" + row["kind"],
            "QQQ",
            max(times),
            micros(row["fetched_at"]),
            micros(archives["observed_at"]),
            0,
            result,
            lineage=(row["raw_sha256"], row["artifact_sha256"]),
        )
    events.sort(key=lambda e: e.order)
    return events, {
        "schema": "liquid-source-normalization-v1",
        "events": len(events),
        "source_export_sha256": digest(records),
        "archive_export_sha256": digest(archives),
        "gaps": gaps,
        "core_sha256": digest([e.content_hash for e in events]),
        "limitations": [
            "Cramer classification created_at is source availability, not executor receipt",
            "GEX export contains selected archived revisions, not the entire archive",
            "source evidence alone lacks native identity/book/margin inputs for full portfolio P&L",
            "source text remains untrusted model/strategy input, never operational instructions",
        ],
    }
