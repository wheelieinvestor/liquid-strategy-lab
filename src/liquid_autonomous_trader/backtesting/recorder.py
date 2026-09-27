"""Bounded free public capture, completely separate from production writers.

Polling captures snapshots, not a complete trade tape. Every restart records a gap;
no missing order-book interval is filled from later snapshots. Each response commits
its evidence and cursor together. Account endpoints and caller-supplied URLs are
unavailable by construction.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from liquid_autonomous_trader.backtesting.events import Catalog, Event, canonical

URL = "https://api.hyperliquid.xyz/info"
MAX_RESPONSE = 4 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect_not_allowed")


def public_info(body: dict):
    if body.get("type") not in {"candleSnapshot", "l2Book", "fundingHistory", "metaAndAssetCtxs"}:
        raise ValueError("unsupported_public_request")
    if set(body) - {"type", "req", "coin", "startTime", "endTime"}:
        raise ValueError("unexpected_request_field")
    request = Request(
        URL,
        data=canonical(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("response_budget_exceeded")
    return json.loads(raw)


def capture(
    path: Path, *, cycles: int = 1, interval: float = 1.0, fetch=public_info, source_spools=()
) -> dict:
    if (
        not 1 <= cycles <= 60
        or not 1 <= interval <= 60
        or len(source_spools) > 8
        or cycles * 4 * (10 + interval) > 1800
    ):
        raise ValueError("capture_budget_exceeded")
    store = Catalog(path)
    started = time.time_ns() // 1000

    def event(kind, payload, *, stamp_us, available_us, quality="observed", sequence=0):
        return Event(
            source="hyperliquid-public-poll-v1",
            instrument="BTC",
            venue="hyperliquid",
            kind=kind,
            event_us=stamp_us,
            published_us=stamp_us,
            available_us=available_us,
            retrieved_us=available_us,
            sequence=sequence,
            revision=0,
            units="USD/base/hour",
            quality=quality,
            reference=URL,
            payload_json=canonical(payload),
        )

    previous = store.cursor("capture")
    gap = {
        "reason": "restart_observation_gap" if previous else "capture_start",
        "previous_capture_us": previous,
        "publication_time": "unavailable_assumed_event_time",
    }
    store.append([event("gap", gap, stamp_us=started, available_us=started, quality="partial")])
    failures = []
    try:
        for cycle in range(cycles):
            for spool in source_spools:
                from liquid_autonomous_trader.backtesting.catalog_io import ingest_spool

                ingest_spool(store, spool)
            now_ms = time.time_ns() // 1_000_000
            requests = [
                (
                    "candle15m",
                    {
                        "type": "candleSnapshot",
                        "req": {
                            "coin": "BTC",
                            "interval": "15m",
                            "startTime": now_ms - 24 * 3600000,
                            "endTime": now_ms,
                        },
                    },
                ),
                ("book", {"type": "l2Book", "coin": "BTC"}),
                (
                    "funding",
                    {
                        "type": "fundingHistory",
                        "coin": "BTC",
                        "startTime": now_ms - 2 * 3600000,
                        "endTime": now_ms,
                    },
                ),
                ("metadata_context", {"type": "metaAndAssetCtxs"}),
            ]
            for kind, body in requests:
                try:
                    response = fetch(body)
                    received = time.time_ns() // 1000
                    rows = response if kind in {"candle15m", "funding"} else [response]
                    entries = []
                    for row in rows:
                        if kind == "candle15m":
                            stamp = int(row["T"]) * 1000
                            if stamp >= received:
                                continue  # unfinished candles are never a completed-bar input
                        elif kind in {"book", "funding"}:
                            stamp = int(row["time"]) * 1000
                        else:
                            stamp = received
                        prior = store.latest("hyperliquid-public-poll-v1", kind, "BTC", stamp)
                        if prior and prior.payload_json == canonical(row):
                            continue
                        item = event(kind, row, stamp_us=stamp, available_us=received)
                        if prior:
                            item = replace(
                                item, revision=prior.revision + 1, lineage=(prior.content_hash,)
                            )
                        entries.append(item)
                    store.append(entries, cursor=(kind, str(received)))
                except (OSError, ValueError, KeyError, TypeError):
                    received = time.time_ns() // 1000
                    failures.append(kind)
                    store.append(
                        [
                            event(
                                "gap",
                                {"reason": "capture_failed", "kind": kind},
                                stamp_us=received,
                                available_us=received,
                                quality="partial",
                            )
                        ]
                    )
                time.sleep(interval)
            store.append([], cursor=("capture", str(time.time_ns() // 1000)))
        return {
            **store.manifest(),
            "cycles": cycles,
            "failed_kinds": failures,
            "observed_at": datetime.now(UTC).isoformat(),
            "limitations": [
                "snapshot polling is not complete tape",
                "publication time approximated by venue event time",
                "arrival time observed locally",
                "no account endpoints",
                "source lanes require explicitly supplied sanitized append-only event spools",
            ],
        }
    finally:
        store.close()
