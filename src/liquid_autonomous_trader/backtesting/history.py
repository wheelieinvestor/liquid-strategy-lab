"""Streaming checksum-verified official free archives; no downloader or paid path."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mmap
import zipfile
from dataclasses import dataclass
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.ledger import number

BASE = "https://data.binance.vision/data/futures/um/"


@dataclass(frozen=True)
class Minute:
    open_us: int
    open: D
    high: D
    low: D
    close: D
    volume: D
    source_hash: str


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_archives(root: Path):
    manifest = json.loads((root / "manifest.json").read_text())
    for source in manifest["sources"]:
        if not source["url"].startswith(BASE):
            # Native funding may appear in other manifest schemas, never assume an archive.
            raise ValueError("non_archive_source_in_archive_manifest")
        relative = source["url"].removeprefix(BASE)
        path = (root / "archives" / relative).resolve()
        if not path.is_relative_to((root / "archives").resolve()):
            raise ValueError("archive_path_escape")
        checksum = path.with_suffix(path.suffix + ".CHECKSUM").read_text().split()[0]
        actual = sha256(path)
        if actual != checksum or actual != source["sha256"]:
            raise ValueError("archive_checksum_mismatch")
        yield path, source


def archive_minutes(path: Path, source_hash: str):
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) != 1 or archive.infolist()[0].file_size > 128 * 1024 * 1024:
            raise ValueError("archive_shape_or_size_budget")
        with archive.open(archive.infolist()[0]) as raw:
            previous = None
            for row in csv.reader(io.TextIOWrapper(raw)):
                if row[0] == "open_time":
                    continue
                timestamp = int(row[0])
                start = timestamp if timestamp > 10**14 else timestamp * 1000
                if start % 60_000_000 or previous is not None and start != previous + 60_000_000:
                    raise ValueError("archive_candle_gap_or_duplicate")
                values = [
                    number(x, positive=i < 4, nonnegative=i == 4) for i, x in enumerate(row[1:6])
                ]
                o, h, lo, c, v = values
                if not lo <= min(o, c) <= max(o, c) <= h:
                    raise ValueError("invalid_archive_ohlc")
                yield Minute(start, o, h, lo, c, v, source_hash)
                previous = start


def audit_archives(root: Path) -> dict:
    entries = []
    for path, source in verified_archives(root):
        count = 0
        first = last = None
        for minute in archive_minutes(path, source["sha256"]):
            count += 1
            first = minute.open_us if first is None else first
            last = minute.open_us
        if count != source["rows"]:
            raise ValueError("archive_manifest_row_count_mismatch")
        entries.append({**source, "first_us": first, "last_us": last, "bytes": path.stat().st_size})
    return {
        "schema": "liquid-archive-audit-v1",
        "sources": entries,
        "original_manifest_sha256": sha256(root / "manifest.json"),
        "original_dataset_file_sha256": sha256(root / "dataset.json"),
        "limitations": [
            "BTCUSDT USD-margined Binance perpetual is proxy for native BTC",
            "checksum proves cached content, not what was published at event time",
            "May-June 2026 funding summaries were already exposed",
            "no historical books or original arrival times in OHLC archives",
        ],
    }


def minutes(root: Path, start_us: int, end_us: int, *, mark=False):
    """Read only trade-candle archives overlapping the requested half-open interval."""
    archives = list(verified_archives(root))
    previous = None
    for path, source in sorted(archives, key=lambda x: x[0].name):
        lane = "/markPriceKlines/" if mark else "/klines/"
        if lane not in source["url"]:
            continue
        # Do not decode prices from unrequested (including reserved) dates merely
        # to discover that an archive does not overlap this development window.
        import re
        from datetime import UTC, datetime, timedelta

        stamp = re.search(r"-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip$", path.name)
        if stamp is None:
            raise ValueError("archive_date_identity_required")
        year, month, day = stamp.groups()
        first = datetime(int(year), int(month), int(day or 1), tzinfo=UTC)
        last = (
            first + timedelta(days=1)
            if day
            else datetime(first.year + (first.month == 12), first.month % 12 + 1, 1, tzinfo=UTC)
        )
        if (
            int(last.timestamp() * 1_000_000) <= start_us
            or int(first.timestamp() * 1_000_000) >= end_us
        ):
            continue
        for minute in archive_minutes(path, source["sha256"]):
            if minute.open_us < start_us:
                continue
            if minute.open_us >= end_us:
                break
            if previous is None and minute.open_us != start_us:
                raise ValueError("requested_history_missing_start")
            if previous is not None and minute.open_us != previous + 60_000_000:
                raise ValueError("requested_history_gap")
            yield minute
            previous = minute.open_us
    if previous is None or previous != end_us - 60_000_000:
        raise ValueError("requested_history_incomplete")


def funding_rates(root: Path) -> dict[int, D]:
    """Validate the pinned canonical dataset then extract its small funding member.

    This avoids loading a million candle dictionaries merely to read funding. The
    cached rates have rounded float provenance; the report retains that limitation.
    """
    manifest = json.loads((root / "manifest.json").read_text())
    path = root / "dataset.json"
    with path.open("rb") as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            content_end = len(data) - 1 if data[-1:] == b"\n" else len(data)
            hasher = hashlib.sha256()
            for offset in range(0, content_end, 1024 * 1024):
                hasher.update(data[offset : min(content_end, offset + 1024 * 1024)])
            if hasher.hexdigest() != manifest["dataset_sha256"]:
                raise ValueError("canonical_dataset_checksum_mismatch")
            key = b'"funding":['
            start = data.find(key)
            if start < 0:
                raise ValueError("missing_funding_member")
            start += len(key) - 1
            end = data.find(b"]", start)
            if end < 0 or end - start > 4 * 1024 * 1024:
                raise ValueError("funding_member_size_budget")
            rows = json.loads(data[start : end + 1], parse_float=D)
    result = {}
    for row in rows:
        stamp = int(row["t"]) * 1000
        if stamp in result:
            raise ValueError("funding_boundary_or_duplicate")
        result[stamp] = number(row["rate"])
    return result
