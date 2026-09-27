"""Local, escaped HTML with explicit missing metrics and economic reconciliation."""

from __future__ import annotations

import html
from collections import Counter
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.ledger import ZERO, serial


def reportable(result):
    if result.get("schema") != "liquid-portfolio-replay-v1":
        return result
    curve = result["equity_curve"]
    reviews = result["jev_reviews"]
    hits = sum(bool(r["result"] and r["result"].get("provenance")) for r in reviews)
    return {
        **result,
        "title": "Liquid shared portfolio research",
        "ledger": result["simulation"]["ledger"],
        "execution": result["simulation"],
        "start_us": curve[0]["at_us"] if curve else 0,
        "end_us": curve[-1]["at_us"] if curve else 0,
        "breaches": [b for row in curve for b in row["breaches"]],
        "core_hash": result["core_sha256"],
        "verdict": "unsupported",
        "fidelity": "Captured-source replay with simulated account and execution",
        "coverage": {"exact_jev_responses": hits, "missing_jev_reviews": len(reviews) - hits},
        "rejection_counts": dict(
            Counter(
                r["reason"]
                for r in result["decisions"]
                if r.get("reason") not in {"accepted", "hold"}
            )
        ),
        "limitations": [
            "Synthetic fixtures measure behavior, never strategy profitability.",
            "Missing historical source/model observations remain unsupported.",
            "Cached model responses only apply to the exact entry, position, plan and context.",
            "Current fee, margin mode and metadata must be bound by the run manifest.",
            "Liquidation execution is unsupported; outcomes stop at a maintenance breach.",
            "Software exits require a later executable book and consume its displayed depth.",
            "No independent paired confidence interval or promotion verdict from this single run.",
        ],
    }


def trade_cohorts(journal):
    open_trades = {}
    completed = []
    for event in journal:
        row = event["data"]
        kind = event["kind"]
        if kind == "fill":
            symbol = row["symbol"]
            quantity, price, fee = map(D, (row["quantity"], row["price"], row["fee"]))
            trade = open_trades.get(symbol)
            if trade is None:
                trade = {
                    "symbol": symbol,
                    "owner": row["owner"],
                    "entry_us": event["at_us"],
                    "quantity": ZERO,
                    "entry": price,
                    "price_pnl": ZERO,
                    "fees": ZERO,
                    "funding": ZERO,
                    "mae": ZERO,
                    "mfe": ZERO,
                }
                open_trades[symbol] = trade
            old = trade["quantity"]
            if old * quantity < 0:
                trade["price_pnl"] += (
                    abs(quantity) * (price - trade["entry"]) * (1 if old > 0 else -1)
                )
            else:
                trade["entry"] = (abs(old) * trade["entry"] + abs(quantity) * price) / abs(
                    old + quantity
                )
            trade["quantity"] += quantity
            trade["fees"] += fee
            if not trade["quantity"]:
                trade["exit_us"] = event["at_us"]
                trade["holding_seconds"] = (event["at_us"] - trade["entry_us"]) / 1_000_000
                trade["net_pnl"] = trade["price_pnl"] - trade["fees"] + trade["funding"]
                completed.append(trade)
                del open_trades[symbol]
        elif kind == "funding" and row["symbol"] in open_trades:
            trade = open_trades[row["symbol"]]
            trade["funding"] -= trade["quantity"] * D(row["oracle_price"]) * D(row["rate"])
        elif kind == "mark" and row["symbol"] in open_trades:
            trade = open_trades[row["symbol"]]
            pnl = trade["quantity"] * (D(row["price"]) - trade["entry"])
            trade["mae"] = min(trade["mae"], pnl)
            trade["mfe"] = max(trade["mfe"], pnl)
    return completed, list(open_trades.values())


def metrics(result: dict) -> dict:
    result = reportable(result)
    state = result["ledger"]
    initial, equity, cash, realized, fees, funding = map(
        D,
        (
            state["initial_cash"],
            state["equity"],
            state["cash"],
            state["realized"],
            state["fees"],
            state["funding"],
        ),
    )
    net = equity - initial
    price_pnl = realized + (equity - cash)
    if abs(net - (price_pnl - fees + funding)) > D("1e-18"):
        raise ValueError("report_ledger_reconciliation_failed")
    peak = initial
    peak_at = result["start_us"]
    drawdown = ZERO
    max_duration = 0
    for row in result["equity_curve"]:
        value = D(row["equity"])
        if value >= peak:
            peak, peak_at = value, row["at_us"]
        else:
            drawdown = max(drawdown, peak - value)
            max_duration = max(max_duration, row["at_us"] - peak_at)
    closed, opened = trade_cohorts(result["ledger_journal"])
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] < 0]
    pnls = sorted(t["net_pnl"] for t in closed)
    # Descriptive empirical tails only. Three overlapping positions cannot yield
    # a credible confidence interval or a population tail estimate.
    tail_count = max(1, (len(pnls) + 19) // 20)
    avg_win = sum(wins, ZERO) / len(wins) if wins else None
    avg_loss = -sum(losses, ZERO) / len(losses) if losses else None
    execution = result["execution"]
    events = execution["events"]
    orders = execution["orders"]
    terminal = [
        o
        for o in orders.values()
        if o["state"]
        in {"filled", "expired", "cancelled", "rejected", "fill_rejected", "cancelled_after_exit"}
    ]
    no_fill = sum(o["fills"] == 0 for o in terminal)
    block_count = (result["end_us"] - result["start_us"]) // (7 * 86400 * 1_000_000)
    return serial(
        {
            "net_pnl": net,
            "price_pnl_at_execution_prices": price_pnl,
            "fees": fees,
            "funding": funding,
            "slippage_informational_already_in_prices": D(state["slippage"]),
            "max_marked_drawdown_usd": drawdown,
            "max_drawdown_duration_seconds": max_duration / 1_000_000,
            "closed_trades": len(closed),
            "open_positions": len(opened),
            "independent_7day_blocks": block_count,
            "win_rate": D(len(wins)) / len(closed) if closed else None,
            "payoff_ratio": avg_win / avg_loss if avg_win is not None and avg_loss else None,
            "expectancy": sum(pnls, ZERO) / len(pnls) if pnls else None,
            "empirical_worst_5pct_trade_mean": sum(pnls[:tail_count], ZERO) / tail_count
            if pnls
            else None,
            "confidence_interval": {
                "status": "insufficient_data",
                "reason": "paired comparison and sufficient independent blocks required",
            },
            "turnover": state["turnover"],
            "average_holding_seconds": sum(t["holding_seconds"] for t in closed) / len(closed)
            if closed
            else None,
            "maintenance_breach_observations": len(result["breaches"]),
            "liquidation_execution": {
                "status": "unsupported",
                "reason": "native mark/book/account history absent",
            },
            "ambiguities": sum(e["kind"] == "intrabar_ambiguity" for e in events),
            "no_fill_rate": D(no_fill) / len(terminal) if terminal else None,
            "protection_failure_exits": sum(e.get("cause") == "protection_failure" for e in events),
            "trades": closed,
            "open_trade_cohorts": opened,
            "mae_mfe_scope": "observed replay marks only, not reconstructed intrabar tape",
            "coverage": result["coverage"],
            "rejections": result["rejection_counts"],
        }
    )


def render(result: dict, path: Path) -> dict:
    if result.get("schema") == "public-offline-contract-v1":
        rows = "".join(
            "<tr><th>" + html.escape(k) + "</th><td>" + html.escape(str(v)) + "</td></tr>"
            for k, v in result.items()
            if k not in {"quote", "contract"}
        )
        path.write_text(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            "<title>Public offline contract research</title>"
            "<style>body{font:16px system-ui;max-width:1000px;margin:40px auto}"
            "th,td{text-align:left;padding:10px;vertical-align:top}</style>"
            "<h1>Public offline contract</h1><p><strong>UNSUPPORTED for strategy "
            "performance.</strong> This fixture checks evidence requirements; "
            "no option trade or P&amp;L is inferred.</p><table>" + rows + "</table></html>"
        )
        return {"net_pnl": None, "closed_trades": 0, "verdict": "unsupported"}
    result = reportable(result)
    values = metrics(result)

    def esc(value):
        return html.escape(str(value))

    summary = {k: v for k, v in values.items() if not isinstance(v, (dict, list))}
    rows = "".join(
        f"<tr><th>{esc(k.replace('_', ' '))}</th><td>"
        f"{esc(v if v is not None else 'undefined / no observations')}</td></tr>"
        for k, v in summary.items()
    )
    limits = "".join(f"<li>{esc(v)}</li>" for v in result["limitations"])
    rejects = "".join(
        f"<tr><td>{esc(k)}</td><td>{v}</td></tr>" for k, v in values["rejections"].items()
    )
    points = result["equity_curve"]
    step = max(1, len(points) // 500)
    sampled = points[::step]
    if sampled:
        lo = min(float(p["equity"]) for p in points)
        hi = max(float(p["equity"]) for p in points)

        def point(i, p):
            x = 20 + i * 760 / max(1, len(sampled) - 1)
            y = 180 - (float(p["equity"]) - lo) * 150 / max(1e-9, hi - lo)
            return f"{x:.1f},{y:.1f}"

        coordinates = " ".join(point(i, p) for i, p in enumerate(sampled))
        chart = (
            '<svg viewBox="0 0 800 210" role="img" aria-label="Marked equity">'
            f'<polyline points="{coordinates}" fill="none" stroke="#086b68" stroke-width="2"/>'
            f'<text x="20" y="205">Equity range ${lo:.2f} to ${hi:.2f}</text></svg>'
        )
    else:
        chart = "<p>No equity observations.</p>"
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Liquid research report</title>
<style>
body{{font:16px system-ui;margin:40px auto;max-width:1000px;padding:0 24px;
color:#182b33;background:#f5f7f5}}h1{{font-size:30px}}
table{{border-collapse:collapse;width:100%;background:white}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #d9e0dd}}th{{width:65%}}
.status{{padding:16px;background:#fff0d6;border-left:4px solid #a06411}}
svg{{background:white;width:100%}}code{{overflow-wrap:anywhere}}li{{margin:8px 0}}
</style>
<h1>{esc(result.get("title", "Liquid BTC historical research"))}</h1>
<p class="status"><strong>{esc(result["verdict"].upper())}</strong> for strategy improvement.
{esc(result["fidelity"])}. This report contains simulated outcomes.</p>
<p>Mode: {esc(result["mode"])} · Core hash: <code>{esc(result["core_hash"])}</code></p>
<h2>Marked equity</h2>{chart}<h2>Economics and execution</h2><table>{rows}</table>
<p>Fees and funding reconcile to ending marked equity. Slippage is already present in
execution-price P&amp;L and is not charged twice. Open endpoint positions remain marked.</p>
<h2>Coverage limits</h2><ul>{limits}</ul>
<p>Exact Jev responses: {values["coverage"]["exact_jev_responses"]};
missing reviews: {values["coverage"]["missing_jev_reviews"]}.
Confidence interval requires a valid paired comparison with enough independent blocks.</p>
<h2>All retained rejection reasons</h2><table>{rejects}</table>
<p>No candidate recommendation or live configuration change follows from this report.</p>
</html>"""
    path.write_text(document)
    return values
