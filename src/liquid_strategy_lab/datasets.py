"""Strict, local-only minute data loading for portable research runs."""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from liquid_autonomous_trader.backtesting.history import Minute, sha256
from liquid_autonomous_trader.backtesting.ledger import number

MINUTE = 60_000_000
MAX_ROWS = 200_000


@dataclass(frozen=True)
class Dataset:
    rows: tuple[Minute, ...]
    description: dict


def timestamp(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("timestamps_must_include_UTC_timezone")
    return int(stamp.timestamp() * 1_000_000)


def load_csv(path: Path, *, kind: str, description: str) -> Dataset:
    if kind not in {"synthetic", "proxy"}:
        raise ValueError("CSV_supports_synthetic_or_proxy_data_not_exact_native_execution")
    if path.stat().st_size > 32 * 1024**2:
        raise ValueError("dataset_exceeds_32_MiB_limit")
    checksum = sha256(path)
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["timestamp", "open", "high", "low", "close", "volume"]:
            raise ValueError("expected_timestamp_open_high_low_close_volume_columns")
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                raise ValueError("dataset_exceeds_200000_rows")
            at = timestamp(row["timestamp"])
            if at % MINUTE or rows and at != rows[-1].open_us + MINUTE:
                raise ValueError("minute_data_must_be_contiguous_sorted_and_unique")
            values = [number(row[k], positive=True) for k in ("open", "high", "low", "close")]
            o, h, low, c = values
            volume = number(row["volume"], nonnegative=True)
            if not low <= min(o, c) <= max(o, c) <= h:
                raise ValueError("invalid_OHLC_prices")
            rows.append(Minute(at, o, h, low, c, volume, checksum))
    if len(rows) < 1470:
        raise ValueError("include_at_least_24_hours_warmup_and_30_minutes_test_data")
    if rows[0].open_us % (15 * MINUTE) or (rows[-1].open_us + MINUTE) % (15 * MINUTE):
        raise ValueError("dataset_must_start_and_end_on_15_minute_boundaries")
    return Dataset(
        tuple(rows),
        {
            "kind": kind,
            "description": description,
            "sha256": checksum,
            "rows": len(rows),
            "funding": "zero_assumed_not_observed",
            "start": datetime.fromtimestamp(rows[0].open_us / 1e6, UTC).isoformat(),
            "end": datetime.fromtimestamp((rows[-1].open_us + MINUTE) / 1e6, UTC).isoformat(),
        },
    )
