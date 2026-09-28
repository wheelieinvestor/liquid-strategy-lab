"""Small cost-free strategy sandbox around the existing Decimal ledger.

Only completed immutable bars and a position snapshot reach the strategy. Signals
execute at the next bar open; the last signal cannot create a fictitious fill.
This is an idealized rule test, not a reconstruction of a Liquid account.
"""

import hashlib
import importlib.util
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.ledger import Instrument, Ledger, Tier, serial
from liquid_strategy_lab.synthetic import BAR_US


@dataclass(frozen=True)
class PositionView:
    side: str
    quantity: D
    entry_price: D | None


def sma_strategy(fast, slow, direction):
    def decide(bars, position):
        if len(bars) < slow:
            return "hold"
        short_average = sum(b.close for b in bars[-fast:]) / fast
        long_average = sum(b.close for b in bars[-slow:]) / slow
        if short_average > long_average:
            return "long"
        if short_average < long_average:
            return "short" if direction == "long_short" else "flat"
        return "hold"

    return decide


def load_custom(path: Path):
    if path.suffix != ".py" or not path.is_file():
        raise ValueError("strategy_file_must_be_a_local_python_file")
    spec = importlib.util.spec_from_file_location("liquid_member_strategy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    decide = getattr(module, "decide", None)
    if not callable(decide):
        raise ValueError("strategy_file_must_define_decide")
    return decide


def run(rows, dataset, settings, decide):
    """A fresh account and fresh callable should be used for every scenario."""
    if len(rows) < 2:
        raise ValueError("at_least_two_bars_required")
    for i, row in enumerate(rows):
        if (
            row.index != i
            or not all(v.is_finite() and v > 0 for v in (row.open, row.high, row.low, row.close))
            or not row.low <= min(row.open, row.close) <= max(row.open, row.close) <= row.high
        ):
            raise ValueError("invalid_or_unordered_synthetic_bars")
    step = D(".00000001")
    instrument = Instrument(
        "SYNTH",
        step,
        step,
        D(0),
        (Tier(D(0), D(1)),),
        "sandbox-fractional-units",
        "synthetic_assumption",
    )
    ledger = Ledger(settings.initial_cash, {"SYNTH": instrument})
    decisions, curve, orders, events = [], [], {}, []
    pending = None
    for i, bar in enumerate(rows):
        ledger.apply(f"open-mark:{i}", bar.open_us, "mark", symbol="SYNTH", price=bar.open)
        if ledger.equity() <= 0:
            raise ValueError("sandbox_equity_exhausted_reduce_position_size")
        position = ledger.positions.get("SYNTH")
        current = "flat" if position is None else "long" if position.quantity > 0 else "short"
        if pending and pending["target"] != current:
            key = f"signal:{pending['bar']}"
            orders[key] = {"state": "filled", "fills": 0}
            if position:
                ledger.apply(
                    f"exit:{i}",
                    bar.open_us,
                    "fill",
                    symbol="SYNTH",
                    owner="member_strategy",
                    quantity=-position.quantity,
                    price=bar.open,
                    fee=D(0),
                    leverage=D(1),
                    mode="cross",
                    reduce_only=True,
                    reference_price=bar.open,
                )
                orders[key]["fills"] += 1
                events.append({"kind": "exit", "at_us": bar.open_us, "cause": "strategy_signal"})
            if pending["target"] != "flat":
                budget = min(settings.position_notional, max(D(0), ledger.available()))
                quantity = (budget / bar.open / step).to_integral_value(rounding=ROUND_FLOOR) * step
                if quantity:
                    ledger.apply(
                        f"entry:{i}",
                        bar.open_us,
                        "fill",
                        symbol="SYNTH",
                        owner="member_strategy",
                        quantity=quantity if pending["target"] == "long" else -quantity,
                        price=bar.open,
                        fee=D(0),
                        leverage=D(1),
                        mode="cross",
                        reference_price=bar.open,
                    )
                    orders[key]["fills"] += 1
                elif not orders[key]["fills"]:
                    orders[key]["state"] = "rejected"
        at = bar.open_us + BAR_US - 1
        ledger.apply(f"close-mark:{i}", at, "mark", symbol="SYNTH", price=bar.close)
        if ledger.equity() <= 0:
            raise ValueError("sandbox_equity_exhausted_reduce_position_size")
        curve.append({"at_us": at, "equity": str(ledger.equity())})
        p = ledger.positions.get("SYNTH")
        view = PositionView(
            "flat" if p is None else "long" if p.quantity > 0 else "short",
            p.quantity if p else D(0),
            p.entry if p else None,
        )
        target = decide(tuple(rows[: i + 1]), view)
        if target not in {"long", "short", "flat", "hold"}:
            raise ValueError("decide_must_return_long_short_flat_or_hold")
        if target == "short" and settings.direction == "long_only":
            raise ValueError("short_signal_conflicts_with_long_only_settings")
        decisions.append(
            {
                "bar": i,
                "at_us": at,
                "target": target,
                "execution": "next_bar_open" if i + 1 < len(rows) else "no_next_bar",
            }
        )
        pending = None if target == "hold" else {"bar": i, "target": target}
    ledger.reconcile()
    result = serial(
        {
            "schema": "liquid-synthetic-sandbox-v1",
            "title": settings.title,
            "mode": "synthetic_sandbox",
            "fidelity": "fictional_scenarios_idealized_execution",
            "start_us": rows[0].open_us,
            "end_us": rows[-1].open_us + BAR_US,
            "assumptions": {
                **settings.model_dump(mode="json"),
                "fee_per_side": "0",
                "spread_bps": "0",
                "slippage_bps": "0",
                "funding": "excluded",
            },
            "dataset": dataset,
            "bars": [asdict(row) for row in rows],
            "ledger": ledger.state(),
            "ledger_journal": ledger.journal,
            "execution": {"orders": orders, "events": events},
            "decisions": decisions,
            "equity_curve": curve,
            "breaches": [],
            "coverage": {"exact_jev_responses": 0, "missing_jev_reviews": 0, "jev_required": False},
            "rejection_counts": dict(
                Counter(o["state"] for o in orders.values() if o["state"] == "rejected")
            ),
            "verdict": "strategy_behavior_only",
            "limitations": [
                "Fictional scenarios, not historical Liquid prices or realistic return forecasts.",
                "Fees, spread, slippage and funding are excluded; results are before all costs.",
                "Completed-bar decisions execute at the next bar open with assumed full liquidity.",
                "Size floors to eight decimals; execution prices receive no cost adjustment.",
                "No intrabar stops, limit orders or venue liquidation model.",
                "Position notional is capped by available cash; no leveraged sizing.",
                "Open endpoint positions stay marked; the final signal cannot fill.",
                "Only past bars reach the strategy; custom Python is trusted, not isolated.",
                "Synthetic data alone does not eliminate look-ahead bugs or selection overfitting.",
            ],
        }
    )
    result["core_hash"] = digest(result)
    return result


def strategy_factory(settings, directory):
    if settings.strategy == "custom":
        path = (directory / settings.strategy_file).resolve()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        return lambda: load_custom(path), checksum
    return lambda: sma_strategy(settings.fast, settings.slow, settings.direction), "builtin-sma-v1"
