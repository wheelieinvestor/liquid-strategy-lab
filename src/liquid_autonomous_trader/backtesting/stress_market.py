"""Joint development-block resampling and explicit conditional market shocks."""

import random
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from functools import lru_cache
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.fixtures import US
from liquid_autonomous_trader.backtesting.history import funding_rates, minutes, sha256
from liquid_autonomous_trader.backtesting.ledger import serial
from liquid_autonomous_trader.backtesting.stress_execution import environment, invariants

START = int(datetime(2025, 7, 2, tzinfo=UTC).timestamp() * 1_000_000)
END = int(datetime(2025, 7, 9, tzinfo=UTC).timestamp() * 1_000_000)


@lru_cache(maxsize=1)
def _prepare(root, manifest_hash):
    root = Path(root)
    price = list(minutes(root, START, END))
    marks = list(minutes(root, START, END, mark=True))
    funding = {}
    for stamp, rate in funding_rates(root).items():
        if START <= stamp < END:
            funding.setdefault(stamp // 60_000_000, []).append((stamp, rate))
    if len(price) != len(marks) or any(p.open_us != m.open_us for p, m in zip(price, marks)):
        raise ValueError("development_trade_mark_join_incomplete")
    rows = [
        {
            "at_us": p.open_us,
            "return": p.close / p.open - 1,
            "range": (p.high - p.low) / p.open,
            "volume": p.volume,
            "mark_return": m.close / m.open - 1,
            "basis": m.close / p.close - 1,
            "funding": funding.get(p.open_us // 60_000_000, []),
        }
        for p, m in zip(price, marks)
    ]
    blocks = [rows[i : i + 30] for i in range(0, len(rows) - 29, 30)]
    values = [sum(row["range"] for row in block) for block in blocks]
    median = sorted(values)[len(values) // 2]
    binding = {
        "start_us": START,
        "end_us": END,
        "minute_rows": len(rows),
        "block_minutes": 30,
        "joint_columns": ["price_return", "range", "volume", "mark_return", "basis", "funding"],
        "subset_sha256": digest(serial(rows)),
        "manifest_sha256": manifest_hash,
        "source_archive_hashes": sorted({p.source_hash for p in price + marks}),
        "quality": "Binance trade/mark proxy joined with native cached funding; not native tape",
        "funding_provenance": "original cached decimalized floats; oracle history unavailable",
    }
    return blocks, values, median, binding


def prepare(root):
    return _prepare(str(Path(root).resolve()), sha256(Path(root) / "manifest.json"))


def execute(case, *, root):
    if case.family != "market":
        raise ValueError("market_case_required")
    blocks, volatility, median, binding = prepare(root)
    rng = random.Random(case.seed)
    indices = list(range(len(blocks)))
    if case.archetype in {"volatility_cluster", "empirical_heavy_tail", "flash_crash_rebound"}:
        indices = [i for i, v in enumerate(volatility) if v >= median]
    elif case.archetype == "chop":
        indices = [i for i, v in enumerate(volatility) if v < median]
    picked = [rng.choice(indices) for _ in range(8)]
    rows = [row for index in picked for row in blocks[index]]
    sim, request = environment(case)
    assert (
        sim.submit(
            "cohort",
            request,
            US,
            mode="cross",
            expires_us=US + 60_000_000,
            latency_us=case.latency_us,
        )
        == "accepted"
    )
    price = D(100)
    step = sim.ledger.instruments["BTC"].price_step
    marks = []
    skipped_funding = 0

    def quote(at, mid, spread, depth, mark):
        half = mid * spread / 20000
        bid = ((mid - half) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        ask = ((mid + half) / step).to_integral_value(rounding=ROUND_CEILING) * step
        sim.quote(
            "BTC", at, bids=[(str(bid), str(depth))], asks=[(str(ask), str(depth))], mark=str(mark)
        )

    quote(US + case.latency_us + 1, price, D(case.spread_bps), D(1000), price)
    assert sim.ledger.positions
    for i, row in enumerate(rows):
        change = row["return"]
        name = case.archetype
        if name in {"trend_long", "trend_funding_drain"}:
            change = abs(change) + D(".0002")
        elif name == "trend_short":
            change = -abs(change) - D(".0002")
        elif name == "chop":
            change = abs(change) * (1 if i % 2 else -1)
        elif name == "false_break_up":
            change = (abs(change) + D(".0005")) * (1 if i < 20 else -1)
        elif name == "false_break_down":
            change = (abs(change) + D(".0005")) * (-1 if i < 20 else 1)
        elif name == "grind_with_friction":
            change = D(".00002")
        elif name == "whipsaw_stops":
            change = D(".004") * (1 if i % 10 < 5 else -1)
        if (
            name
            in {
                "flash_crash_rebound",
                "opening_gap",
                "weekend_gap",
                "shock_spread_latency",
                "correlated_selloff",
            }
            and i == 30
        ):
            change -= D(".08") * D(case.shock_scale)
        if name == "flash_crash_rebound" and i == 31:
            change += D(".08") * D(case.shock_scale)
        if name == "short_squeeze" and i == 30:
            change += D(".10") * D(case.shock_scale)
        price = max(D(1), price * (1 + change))
        spread = D(case.spread_bps) * (1 + row["range"] * 100)
        depth = max(
            D(".01"), (D(20) * D(case.depth_fraction) / (1 + row["range"] * 100)).quantize(D(".01"))
        )
        if name == "depleted_depth":
            depth = D(".01") * (case.variant + 1)
        mark = price * (1 + row["basis"])
        if name == "mark_book_divergence" and i >= 30:
            mark *= D(".95")
        at = US + (i + 1) * 60_000_000
        if name == "weekend_gap" and i >= 30:
            at += 48 * 3600 * 1_000_000
        quote(at, price, spread, depth, mark)
        for original_stamp, rate in row["funding"]:
            if name == "stale_oracle":
                skipped_funding += 1
                continue
            # Synthetic economic settlement has a new timestamp; original source
            # time remains provenance. Oracle=proxy mid is an explicit assumption.
            rate = rate * (10 if name == "trend_funding_drain" else 1)
            if name == "funding_flip":
                rate = abs(rate) * (1 if i < 120 else -1)
            # Rebased hourly settlements may collide when concatenating blocks;
            # retain at most one settlement per synthetic account-hour.
            key = ("BTC", at // 3_600_000_000)
            if key not in sim.ledger.funding_settlements:
                sim.ledger.apply(
                    "funding:" + str(i),
                    at,
                    "funding",
                    symbol="BTC",
                    oracle_price=price,
                    rate=rate,
                    settlement_us=at,
                    native_timestamp=True,
                )
        marks.append(
            {
                "at_us": at,
                "equity": str(sim.ledger.equity()),
                "mid": str(price),
                "mark": str(mark),
                "conditional_depth": str(depth),
                "source_us": row["at_us"],
            }
        )
        if sim.ledger.breaches():
            break
    checks = invariants(sim)
    assert all(START <= row["source_us"] < END for row in marks)
    checks.extend(
        ["development_only_joint_blocks", "fixed_seed_resampling", "no_gaussian_tape_claim"]
    )
    result = {
        "case_id": case.case_id,
        "seed": case.seed,
        "configuration": asdict(case),
        "status": "passed",
        "mode": "synthetic_stress",
        "checks": checks,
        "block_indices": picked,
        "data_binding": binding,
        "equity_curve": marks,
        "state": sim.state(),
        "skipped_missing_oracle_settlements": skipped_funding,
        "termination": "unsupported_liquidation_execution"
        if sim.ledger.breaches()
        else "end_of_conditional_path",
        "limitations": [
            "Authored matched entry, not evidence of the production entry edge",
            "Liquidity response is an assumption; no historical books exist",
            "Price/mark/range/volume/funding sampled jointly within 30-minute blocks",
            "Trend, shock, spread and depth transformations are explicitly counterfactual",
            "No empirically supported shock frequency or native liquidation fill",
        ],
    }
    return {**result, "core_hash": digest(result)}
