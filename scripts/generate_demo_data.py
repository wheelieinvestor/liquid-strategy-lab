"""Rebuild our authored, CC0 synthetic teaching dataset with Decimal arithmetic."""

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / "src/liquid_strategy_lab/data/btc-demo.csv"
start = datetime(2025, 1, 1, tzinfo=UTC)
price = D("100000")
# Thirty-two 1-hour regimes, chosen to illustrate both continuation and reversals.
regimes = [D(x) for x in [".003", ".003", "-.004", ".001", "-.003", "-.003", ".004", "-.001"]] * 4
with path.open("w", newline="", encoding="utf-8") as stream:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for i in range(1440 + len(regimes) * 60):
        if i < 1440:
            movement = D(".00001") * (1 if (i // 15) % 2 else -1)
            volume = D("2")
        else:
            hour = (i - 1440) // 60
            movement = regimes[hour] / 15
            volume = D("5") if hour % 2 == 0 else D("2")
        close = (price * (1 + movement)).quantize(D(".01"))
        high, low = max(price, close) + D("3"), min(price, close) - D("3")
        writer.writerow(
            [(start + timedelta(minutes=i)).isoformat(), price, high, low, close, volume]
        )
        price = close
print(f"Generated {path.name}: 24h warmup and 32h synthetic trading, no historical observations")
