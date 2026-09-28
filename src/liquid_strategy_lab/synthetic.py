"""Repeatable fictional price paths; no fitted market data or network capability."""

import random
from dataclasses import asdict, dataclass
from decimal import Decimal as D

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.ledger import serial

SCENARIOS = ("uptrend", "downtrend", "sideways", "choppy", "sudden-drop")
BAR_US = 15 * 60 * 1_000_000
START_US = 1735689600000000  # Fictional timeline, not January 2025 market history.


@dataclass(frozen=True)
class Bar:
    index: int
    open: D
    high: D
    low: D
    close: D

    @property
    def open_us(self):
        return START_US + self.index * BAR_US


def generate(scenario: str, seed: int, count: int) -> tuple[Bar, ...]:
    if scenario not in SCENARIOS or not 300 <= count <= 10000:
        raise ValueError("unknown_scenario_or_bar_count_outside_300_to_10000")
    rng = random.Random(f"liquid-synthetic-v1:{scenario}:{seed}")
    anchor = price = D("10000")
    rows = []
    precision = D(".00000001")
    for i in range(count):
        noise = D(rng.randint(-1000, 1000)) / 1_000_000
        opening = price * (D(".75") if scenario == "sudden-drop" and i == 256 else 1)
        drift = {
            "uptrend": D(".0012"),
            "downtrend": D("-.0012"),
            "sideways": (anchor / opening - 1) * D(".04"),
            "choppy": D(".006") * (1 if (i // 8) % 2 == 0 else -1),
            "sudden-drop": D(".0004"),
        }[scenario]
        if scenario == "sideways":
            noise *= 6
        closing = (opening * (1 + drift + noise)).quantize(precision)
        opening = opening.quantize(precision)
        wick = opening * abs(noise) / 2
        rows.append(
            Bar(
                i,
                opening,
                (max(opening, closing) + wick).quantize(precision),
                (min(opening, closing) - wick).quantize(precision),
                closing,
            )
        )
        price = closing
    return tuple(rows)


def description(scenario, seed, rows):
    return {
        "kind": "synthetic",
        "description": f"Fictional {scenario} scenario; seed {seed}; not historical prices.",
        "scenario": scenario,
        "generator": "liquid-synthetic-v1",
        "seed": seed,
        "rows": len(rows),
        "timeframe": "15m",
        "funding": "excluded",
        "sha256": digest(serial([asdict(row) for row in rows])),
    }
