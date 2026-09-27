"""One historical BTC vertical slice through production entries and shared execution.

This is conditional proxy research. Binance candles cannot establish native book
quality, oracle prices, actual margin mode or exact Jev counterfactual decisions.
Those assumptions are explicit run inputs and outputs, never historical facts.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.admission import btc_request, utc
from liquid_autonomous_trader.backtesting.events import canonical, digest
from liquid_autonomous_trader.backtesting.execution import Order, SimulatedExecution
from liquid_autonomous_trader.backtesting.history import Minute, funding_rates, minutes
from liquid_autonomous_trader.backtesting.ledger import (
    FeeSchedule,
    Instrument,
    Ledger,
    Tier,
    serial,
)
from liquid_autonomous_trader.btc_features import build_features_v2
from liquid_autonomous_trader.btc_source import Candle
from liquid_autonomous_trader.frozen.models import MarketContextV1, SignalAction
from liquid_autonomous_trader.frozen.strategies.btc_momentum import (
    BtcMomentumConfigV1,
    BtcMomentumEngineV1,
    BtcMomentumObservationV1,
)

MINUTE_US = 60_000_000


@dataclass(frozen=True)
class HistoryAssumptions:
    fee_per_side: str = "0.000682"
    spread_bps: str = "1"
    extra_slippage_bps: str = "0"
    depth_base: str = "1"
    initial_cash: str = "1000"
    margin_mode: str = "cross"
    native_quantity_step: str = "0.00001"
    native_price_step: str = "1"
    native_max_leverage: str = "40"
    latency_us: int = 1_000_000
    policy: str = "current-v5-missing-cache-hold"
    adverse_intrabar: bool = True


def aggregate(rows: list[Minute]) -> Candle:
    if (
        len(rows) != 15
        or rows[0].open_us % (15 * MINUTE_US)
        or any(b.open_us - a.open_us != MINUTE_US for a, b in zip(rows, rows[1:]))
    ):
        raise ValueError("incomplete_15m_bar")
    return Candle(
        t=rows[0].open_us // 1000,
        T=(rows[-1].open_us + MINUTE_US) // 1000 - 1,
        s="BTC",
        i="15m",
        o=rows[0].open,
        h=max(r.high for r in rows),
        l=min(r.low for r in rows),
        c=rows[-1].close,
        v=sum((r.volume for r in rows), D(0)),
        n=0,
    )


def historical_observation(bar, features, now, assumptions):
    spread = max(D(assumptions.native_price_step) * 2, bar.c * D(assumptions.spread_bps) / 10000)
    return BtcMomentumObservationV1(
        market=MarketContextV1(
            symbol="BTC",
            observed_at=utc(now),
            price=bar.c,
            atr_15m=features.atr14_price,
            spread_price=spread,
            spread_bps=spread / bar.c * 10000,
            depth_multiple=D(assumptions.depth_base) * bar.c / 2000,
        ),
        return_15m=features.return_1bar,
        return_1h=features.return_4bar,
        trend_efficiency=features.efficiency_4bar,
        volume_acceleration=features.volume_acceleration,
        funding_rate=None,
        persistence_bars=features.persistence_bars,
        data_complete=True,
    )


def run_btc(
    root: Path,
    start_us: int,
    end_us: int,
    assumptions: HistoryAssumptions,
    *,
    matched_entry=None,
    resource_guard=None,
    minute_rows=None,
    funding=None,
    dataset=None,
) -> dict:
    if assumptions.policy not in {
        "current-v5-missing-cache-hold",
        "legacy-production",
        "breakeven-1R",
        "breakeven-1.25R",
        "breakeven-1.5R",
    }:
        raise ValueError("unsupported_historical_policy")
    if end_us <= start_us or start_us % (15 * MINUTE_US) or end_us % (15 * MINUTE_US):
        raise ValueError("aligned_history_window_required")
    spec = Instrument(
        "BTC",
        D(assumptions.native_quantity_step),
        D(assumptions.native_price_step),
        D(10),
        (Tier(D(0), D(assumptions.native_max_leverage)),),
        digest(asdict(assumptions)),
        "synthetic_assumption",
    )
    ledger = Ledger(D(assumptions.initial_cash), {"BTC": spec})
    fee = FeeSchedule(
        "historical-lead-not-account-calibration-v1",
        "liquid-route-assumption",
        "unknown",
        "unknown",
        start_us,
        end_us,
        D(assumptions.fee_per_side),
        D(assumptions.fee_per_side),
        "synthetic_assumption",
    )
    sim = SimulatedExecution(ledger, fee, adverse_intrabar=assumptions.adverse_intrabar)
    manager = None
    if assumptions.policy != "current-v5-missing-cache-hold":
        from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine

        manager = PortfolioEngine(
            sim,
            policy=assumptions.policy,
            enabled=(),
            margin_mode=assumptions.margin_mode,
            latency_us=assumptions.latency_us,
            mode="historical_execution_simulation",
        )
    if matched_entry is not None:
        order = matched_entry["order"]
        fill = matched_entry["fill"]
        if (
            order["fills"] != 1
            or D(fill["data"]["quantity"]) != D(order["signed_quantity"])
            or not start_us <= fill["at_us"] < end_us
            or fill["at_us"] % MINUTE_US
            or fill["data"]["mode"] != assumptions.margin_mode
        ):
            raise ValueError("matched_entry_requires_one_complete_fill_in_window_and_same_margin")

    def register_plan(owner, plan):
        if manager is not None:
            manager.plans[owner] = plan
            manager.account.db.execute(
                "INSERT OR REPLACE INTO btc_runtime_decisions VALUES(?,?)", (owner, canonical(plan))
            )

    rates = funding_rates(root) if funding is None else funding
    rate_items = iter(sorted((t, r) for t, r in rates.items() if start_us <= t < end_us))
    funding = next(rate_items, None)
    candles = deque(maxlen=96)
    bucket = []
    decisions = []
    equity = []
    breaches = []
    source_hashes = set()
    strategy = BtcMomentumEngineV1(BtcMomentumConfigV1(funding_filter_enabled=False))
    warmup = start_us - 24 * 60 * MINUTE_US
    missing_models = 0
    rows = minutes(root, warmup, end_us) if minute_rows is None else minute_rows
    for index, row in enumerate(rows):
        if resource_guard is not None and index % 1000 == 0:
            resource_guard()
        source_hashes.add(row.source_hash)
        row_end = row.open_us + MINUTE_US - 1
        if row.open_us >= start_us:
            # Entry executes only at a later recorded minute open. Spread and depth
            # below are declared synthetic overlays, not observed Binance/HL books.
            half = D(assumptions.spread_bps) / 20000 + D(assumptions.extra_slippage_bps) / 10000
            bid = (row.open * (1 - half) / spec.price_step).to_integral_value(
                rounding=ROUND_FLOOR
            ) * spec.price_step
            ask = (row.open * (1 + half) / spec.price_step).to_integral_value(
                rounding=ROUND_CEILING
            ) * spec.price_step
            # Funding at exactly this open occurs before new fills. Delayed native
            # settlements within the minute use the same causal position inventory.
            while funding is not None and funding[0] <= row.open_us:
                t, rate = funding
                ledger.apply(
                    f"funding:{t}",
                    t,
                    "funding",
                    symbol="BTC",
                    oracle_price=row.open,
                    rate=rate,
                    settlement_us=t,
                    native_timestamp=True,
                )
                funding = next(rate_items, None)
            sim.quote(
                "BTC",
                row.open_us,
                bids=[(bid, assumptions.depth_base)],
                asks=[(ask, assumptions.depth_base)],
                mark=row.open,
            )
            if matched_entry is not None and row.open_us == matched_entry["fill"]["at_us"]:
                # Exit-only matched cohorts take exactly the same simulated
                # starting economics. They do not claim independent entry alpha.
                data = dict(matched_entry["fill"]["data"])
                data.pop("order_id", None)
                ledger.apply("matched-entry", row.open_us, "fill", **data)
                order_data = dict(matched_entry["order"])
                for key in ("signed_quantity", "remaining", "stop", "target", "leverage", "limit"):
                    if order_data[key] is not None:
                        order_data[key] = D(order_data[key])
                order_data.update(state="filled", remaining=D(0), fills=1)
                order = Order(**order_data)
                sim.orders[order.order_id] = order
                ledger.apply(
                    "matched-original-stop",
                    row.open_us,
                    "stop",
                    symbol="BTC",
                    owner=order.owner,
                    price=order.stop,
                    original=True,
                )
                register_plan(order.order_id, matched_entry["plan"])
            while funding is not None and funding[0] <= row_end:
                t, rate = funding
                ledger.apply(
                    f"funding:{t}",
                    t,
                    "funding",
                    symbol="BTC",
                    oracle_price=row.open,
                    rate=rate,
                    settlement_us=t,
                    native_timestamp=True,
                )
                funding = next(rate_items, None)
            sim.bar(
                "BTC",
                row.open_us,
                row_end,
                open_price=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
            )
            equity.append({"at_us": row_end, "equity": str(ledger.equity())})
            if ledger.breaches():
                breaches.append({"at_us": row_end, "details": ledger.breaches()})
                # No native liquidation tape: stop reporting portfolio survival or
                # later returns as validated after the first maintenance breach.
                break
        bucket.append(row)
        if len(bucket) != 15:
            continue
        bar = aggregate(bucket)
        bucket = []
        candles.append(bar)
        if row.open_us < start_us:
            continue
        now = row_end
        features = build_features_v2(list(candles))
        if ledger.positions:
            if manager is None:
                missing_models += 1
                decisions.append(
                    {
                        "at_us": now,
                        "action": "hold",
                        "reason": "jev_exact_cache_missing_preserve_stop_quantity",
                    }
                )
            else:
                observation = historical_observation(bar, features, now, assumptions)
                manager.inputs.advance(now)
                manager.btc_manager.source = lambda: ("historical", observation)
                manager.tick()
                decisions.append(
                    {
                        "at_us": now,
                        "action": "manage",
                        "reason": manager.decisions[-1]["reason"]
                        if manager.decisions
                        else "no_management",
                    }
                )
            continue
        if matched_entry is not None:
            continue  # no counterfactual re-entry in an exit-only matched cohort
        observation = historical_observation(bar, features, now, assumptions)
        signal = strategy.evaluate(observation)
        record = {
            "at_us": now,
            "action": signal.action.value,
            "reasons": list(signal.reason_codes),
            "features": features.model_dump(mode="json"),
            "signal": signal.model_dump(mode="json"),
        }
        if signal.action == SignalAction.ENTER:
            req = btc_request(ledger, observation, signal, now)
            record["admission"] = sim.submit(
                signal.signal_id,
                req,
                now,
                mode=assumptions.margin_mode,
                expires_us=now + 15 * MINUTE_US,
                latency_us=assumptions.latency_us,
            )
            if signal.signal_id in sim.orders:
                register_plan(
                    signal.signal_id,
                    {
                        "target": str(signal.bracket.target_price),
                        "tick_size": str(spec.price_step),
                        "entry_observation": observation.model_dump(mode="json"),
                    },
                )
        decisions.append(record)
    ledger.reconcile()
    reasons = Counter(r for d in decisions for r in d.get("reasons", [d.get("reason", "")]) if r)
    result = {
        "schema": "liquid-btc-historical-v1",
        "mode": "historical_execution_simulation",
        "fidelity": "proxy_with_synthetic_assumptions",
        "start_us": start_us,
        "end_us": end_us,
        "assumptions": asdict(assumptions),
        "matched_entry": matched_entry,
        "management_decisions": manager.decisions if manager is not None else [],
        "data_hashes": sorted(source_hashes),
        "ledger": ledger.state(),
        "execution": sim.state(),
        "decisions": decisions,
        "equity_curve": equity,
        "ledger_journal": ledger.journal,
        "breaches": breaches,
        "coverage": {
            "exact_jev_responses": 0,
            "missing_jev_reviews": missing_models,
            "native_book_observations": 0,
            "native_oracle_observations": 0,
        },
        "rejection_counts": dict(reasons),
        "verdict": "unsupported",
        "limitations": [
            "Binance BTCUSDT proxy, not Liquid execution",
            "assumed spread/depth/precision/current margin tier, not historical observations",
            "current v5 uses missing-model fallback; exact counterfactual Jev coverage absent",
            "management sampled on completed 15m bars; intraminute native protection still modeled",
            "legacy management calls production code; breakeven policies "
            "are frozen research candidates",
            "matched entry mode disables subsequent entries and marks open endpoint positions",
            "native funding times preserved; oracle approximated by proxy minute open",
            "funding/stop ordering within a minute is ambiguous",
            "funding telemetry disabled for entry per mandate; no historical zero-funding claim",
            "fees are a historical lead, not account/route calibrated for this interval",
            "maintenance breaches stop the run; native liquidation execution unverified",
            "open endpoint positions marked, never silently forced closed",
        ],
    }
    if dataset is not None:
        result["dataset"] = dataset
        result["mode"] = (
            "synthetic_stress"
            if dataset["kind"] == "synthetic"
            else "historical_execution_simulation"
        )
        result["fidelity"] = dataset["kind"] + "_with_synthetic_execution_assumptions"
        result["limitations"][0] = dataset["description"]
        result["limitations"][6] = (
            "Funding is supplied by the dataset; zero synthetic funding is a scenario assumption"
        )
    result = serial(result)
    if manager is not None:
        manager.close()
    result["core_hash"] = digest(result)
    return result
