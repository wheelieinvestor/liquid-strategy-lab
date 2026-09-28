"""Independently check saved run economics using signed cash flows and inventory.

This intentionally does not import the engine ledger, trade metrics, or reports.
It checks arithmetic for the saved assumptions, not historical execution fidelity.
"""

import argparse
import csv
import json
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

ZERO = D(0)
TOLERANCE = D("1e-18")


def verify(result):
    portfolio = result["schema"] == "liquid-portfolio-replay-v1"
    state = result["simulation"]["ledger"] if portfolio else result["ledger"]
    # The authored four-agent fixture uses a declared .095% all-in taker rate.
    rate = D(".00095") if portfolio else D(result["assumptions"]["fee_per_side"])
    initial = D(state["initial_cash"])
    cash_flow = fees = funding = turnover = friction = ZERO
    inventory, basis, marks = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO), {}
    settlements = set()
    fills, observed, closed = [], {}, []
    trade_cash, trade_fees, trade_funding = (defaultdict(lambda: ZERO) for _ in range(3))
    realized = ZERO
    checks = 0

    def equal(label, actual, expected):
        nonlocal checks
        if abs(D(actual) - expected) > TOLERANCE:
            raise AssertionError(f"{label}: saved={actual}, independently computed={expected}")
        checks += 1

    def marked_equity():
        return (
            initial
            + cash_flow
            - fees
            + funding
            + sum((q * marks[s] for s, q in inventory.items() if q), ZERO)
        )

    for event in result["ledger_journal"]:
        data, at = event["data"], event["at_us"]
        kind = event["kind"]
        symbol = data.get("symbol")
        if kind == "fill":
            q, price, charged = map(D, (data["quantity"], data["price"], data["fee"]))
            expected_fee = abs(q * price) * rate
            # Matched-entry experiments intentionally retain the baseline fee.
            if event["id"] != "matched-entry":
                equal("fill fee " + event["id"], charged, expected_fee)
            cash_flow -= q * price
            previous = inventory[symbol]
            if previous * q < 0:
                average = basis[symbol] / previous
                realized += -q * (price - average)
                basis[symbol] += q * average
            else:
                basis[symbol] += q * price
            inventory[symbol] += q
            marks.setdefault(symbol, price)
            fees += charged
            trade_cash[symbol] -= q * price
            trade_fees[symbol] += charged
            if not inventory[symbol]:
                closed.append(
                    trade_cash.pop(symbol)
                    - trade_fees.pop(symbol)
                    + trade_funding.pop(symbol, ZERO)
                )
            turnover += abs(q * price)
            reference = D(data["reference_price"]) if "reference_price" in data else None
            cost = q * (price - reference) if reference is not None else ZERO
            friction += cost
            fills.append(
                {
                    "id": event["id"],
                    "at_us": at,
                    "symbol": symbol,
                    "quantity": str(q),
                    "price": str(price),
                    "reference_price": str(reference) if reference is not None else "",
                    "notional": str(abs(q * price)),
                    "fee": str(charged),
                    "combined_execution_friction": str(cost),
                    "reduce_only": data.get("reduce_only", False),
                }
            )
        elif kind == "mark":
            marks[symbol] = D(data["price"])
        elif kind == "funding":
            settlement = (symbol, data["settlement_us"] // 3_600_000_000)
            if settlement not in settlements:
                payment = -inventory[symbol] * D(data["oracle_price"]) * D(data["rate"])
                funding += payment
                trade_funding[symbol] += payment
                settlements.add(settlement)
        observed[at] = marked_equity()

    for label, expected in [
        ("equity", marked_equity()),
        ("fees", fees),
        ("funding", funding),
        ("turnover", turnover),
        ("slippage", friction),
        ("realized", realized),
    ]:
        equal(label, state[label], expected)
    equal("cash reconciliation", state["cash"], initial + D(state["realized"]) - fees + funding)
    for symbol in set(inventory) | set(state["positions"]):
        equal(
            "ending quantity " + symbol,
            state["positions"].get(symbol, {}).get("quantity", "0"),
            inventory[symbol],
        )
        if inventory[symbol]:
            equal(
                "average entry " + symbol,
                state["positions"][symbol]["entry"],
                basis[symbol] / inventory[symbol],
            )

    stamps = sorted(observed)
    i, current, peak, drawdown = 0, initial, initial, ZERO
    for point in result["equity_curve"]:
        while i < len(stamps) and stamps[i] <= point["at_us"]:
            current = observed[stamps[i]]
            i += 1
        equal("equity curve " + str(point["at_us"]), point["equity"], current)
        peak = max(peak, current)
        drawdown = max(drawdown, peak - current)
    return {
        "status": "passed",
        "checks": checks,
        "fills": len(fills),
        "net_pnl": str(marked_equity() - initial),
        "fees": str(fees),
        "funding": str(funding),
        "combined_execution_friction": str(friction),
        "max_marked_drawdown_usd": str(drawdown),
        "closed_trades": len(closed),
        "win_rate": str(D(sum(p > 0 for p in closed)) / len(closed)) if closed else None,
        "expectancy": str(sum(closed, ZERO) / len(closed)) if closed else None,
        "fills_without_cost_reference": sum(not f["reference_price"] for f in fills),
    }, fills


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Folder containing saved run JSON files")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if any(args.output.iterdir()):
        parser.error("Choose a new empty output directory")
    summaries, all_fills = {}, []
    for path in sorted(args.directory.rglob("*.json")):
        # A complete comparison can contain very large raw run artifacts.
        if path.stat().st_size > 64 * 1024**2:
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("schema") not in {
            "liquid-btc-historical-v1",
            "liquid-portfolio-replay-v1",
            "liquid-synthetic-sandbox-v1",
        }:
            continue
        name = str(path.relative_to(args.directory))
        summary, fills = verify(result)
        comparison = path.parent / "comparison.json"
        if comparison.exists():
            saved = json.loads(comparison.read_text(encoding="utf-8"))
            values = saved.get("metrics", {}).get(path.stem)
            if values is None:
                values = next(
                    (
                        r.get("metrics")
                        for r in saved.get("runs", [])
                        if r.get("artifact") == path.name
                    ),
                    None,
                )
            if values:
                for field in (
                    "net_pnl",
                    "fees",
                    "funding",
                    "max_marked_drawdown_usd",
                    "closed_trades",
                    "win_rate",
                    "expectancy",
                ):
                    actual, expected = values[field], summary[field]
                    if actual is None or expected is None:
                        assert actual is expected, field
                    else:
                        assert abs(D(actual) - D(expected)) <= TOLERANCE, (name, field)
                    summary["checks"] += 1
        summaries[name] = summary
        all_fills.extend({"run": name, **fill} for fill in fills)
    if not summaries:
        parser.error("No supported saved runs found")
    report = {
        "status": "passed",
        "runs": len(summaries),
        "fills": len(all_fills),
        "checks": sum(s["checks"] for s in summaries.values()),
        "scope": "saved synthetic sandbox, all-taker BTC and authored portfolio runs; "
        "not proof of observed fees, execution or profitability",
        "results": summaries,
    }
    (args.output / "arithmetic.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    if all_fills:
        with (args.output / "fills.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(all_fills[0]))
            writer.writeheader()
            writer.writerows(all_fills)
    print(
        f"PASS: {report['runs']} runs, {report['fills']} fills, "
        f"{report['checks']} arithmetic checks"
    )


if __name__ == "__main__":
    main()
