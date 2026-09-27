"""Immutable archive export and restartable forwarding of sanitized source events."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import Catalog, Event, canonical, digest

SOURCE_KINDS = {
    "flow_delivery",
    "gamma_matrix",
    "gamma_spot",
    "cramer_classification",
    "market_identity",
    "jev_request",
    "source_gap",
}
MAX_LINE = 4 * 1024 * 1024
MAX_SPOOL = 64 * 1024 * 1024


def ingest_spool(store: Catalog, path: Path, *, max_events=1000):
    """Forward an explicitly supplied append-only JSONL file, with cursor integrity.

    Producer events must already carry original publication/arrival times. This
    reads no production database and never advances a production source cursor.
    A partial final line waits for the producer's next append. Prefix changes or
    truncation fail instead of silently replacing previously observed evidence.
    """
    if not 1 <= max_events <= 10000 or path.stat().st_size > MAX_SPOOL:
        raise ValueError("source_spool_budget")
    key = "spool:" + digest(str(path.resolve()))
    saved = json.loads(store.cursor(key) or '{"offset":0,"prefix_sha256":null}')
    count = inserted = 0
    with path.open("rb") as stream:
        prefix = stream.read(saved["offset"])
        if (
            len(prefix) != saved["offset"]
            or saved["offset"]
            and hashlib.sha256(prefix).hexdigest() != saved["prefix_sha256"]
        ):
            raise ValueError("source_spool_prefix_changed")
        hasher = hashlib.sha256(prefix)
        while count < max_events:
            line = stream.readline(MAX_LINE + 1)
            if not line:
                break
            if len(line) > MAX_LINE:
                raise ValueError("source_event_size_budget")
            if not line.endswith(b"\n"):
                break
            value = json.loads(line)
            value["lineage"] = tuple(value.get("lineage", []))
            event = Event(**value)
            if event.kind not in SOURCE_KINDS:
                raise ValueError("source_spool_lane_not_allowed")
            hasher.update(line)
            cursor = canonical({"offset": stream.tell(), "prefix_sha256": hasher.hexdigest()})
            inserted += store.append([event], cursor=(key, cursor))
            count += 1
    return {"processed": count, "inserted": inserted, "source_cursor": store.cursor(key)}


def export_catalog(store: Catalog, path: Path):
    """Coherent transactional export. Existing archives may never be overwritten."""
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    if path.exists() or manifest_path.exists():
        raise ValueError("immutable_export_destination_exists")
    temporary = path.with_suffix(path.suffix + ".partial")
    hasher, count, byte_count = hashlib.sha256(), 0, 0
    first = last = None
    store.db.execute("BEGIN")
    try:
        with temporary.open("xb") as output:
            for event in store.replay(until_us=2**62):
                line = (canonical(asdict(event)) + "\n").encode()
                byte_count += len(line)
                if len(line) > MAX_LINE or byte_count > store.max_bytes * 2:
                    raise ValueError("export_size_budget")
                output.write(line)
                hasher.update(line)
                count += 1
                first = event.available_us if first is None else first
                last = event.available_us
            output.flush()
            os.fsync(output.fileno())
        manifest = {
            **store.manifest(),
            "schema": "liquid-event-export-v1",
            "jsonl_sha256": hasher.hexdigest(),
            "bytes": byte_count,
            "rows": count,
            "first_available_us": first,
            "last_available_us": last,
            "retention": "immutable archive; source catalog preserved; stop collection at disk cap",
        }
        # Link with exclusive destination semantics instead of overwriting a
        # concurrent export. A leftover partial file requires an explicit new name.
        os.link(temporary, path)
        temporary.unlink()
        with manifest_path.open("x") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return manifest
    finally:
        store.db.rollback()


def verify_export(path: Path):
    manifest = json.loads(path.with_suffix(path.suffix + ".manifest.json").read_text())
    if manifest["schema"] != "liquid-event-export-v1" or path.stat().st_size != manifest["bytes"]:
        raise ValueError("export_manifest_shape_or_size")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    if hasher.hexdigest() != manifest["jsonl_sha256"]:
        raise ValueError("export_hash_mismatch")
    return manifest


def health(store: Catalog, *, now_us: int):
    kinds = {}
    gaps = {}
    for event in store.replay(until_us=2**62):
        key = event.instrument + ":" + event.kind
        item = kinds.setdefault(
            key,
            {
                "count": 0,
                "first_available_us": event.available_us,
                "last_available_us": event.available_us,
                "last_event_us": event.event_us,
            },
        )
        item["count"] += 1
        item["last_available_us"] = max(item["last_available_us"], event.available_us)
        item["last_event_us"] = max(item["last_event_us"], event.event_us)
        if event.kind in {"gap", "source_gap"}:
            reason = event.payload.get("reason", "unspecified")
            gaps[reason] = gaps.get(reason, 0) + 1
    for item in kinds.values():
        item["age_seconds"] = (now_us - item["last_available_us"]) / 1_000_000
    return {
        **store.manifest(),
        "lanes": kinds,
        "gaps": gaps,
        "catalog_bytes": store.path.stat().st_size,
        "catalog_byte_cap": store.max_bytes,
        "service_running": "not_inferred_from_checkpoint; foreground capture only",
        "stop": "Ctrl-C in the foreground recorder terminal",
        "retention": "stop at cap; export immutable partitions; retain originals; "
        "start new catalog",
    }
