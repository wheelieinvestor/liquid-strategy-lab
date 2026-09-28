"""Readable, self-contained comparison reports without remote scripts or assets."""

import html
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.report import metrics, reportable

LABELS = {
    "uptrend": "Rising market",
    "downtrend": "Falling market",
    "sideways": "Sideways market",
    "choppy": "Choppy market",
    "sudden-drop": "Sudden drop",
    "preserve-stop": "Preserve the original stop",
    "legacy-exits": "Legacy strategy exits",
    "breakeven-1R": "Move stop to entry at 1R",
    "breakeven-1.25R": "Move stop to entry at 1.25R",
    "breakeven-1.5R": "Move stop to entry at 1.5R",
    "portfolio": "Shared account",
}
COLORS = ["#16786b", "#c06427", "#6953a0", "#2369a5", "#bd4666"]


def esc(value):
    return html.escape(str(value), quote=True)


def money(value):
    return f"${D(value):,.2f}" if value is not None else "Unavailable"


def chart(results):
    curves = [reportable(r)["equity_curve"] for r in results.values()]
    points = [point for curve in curves for point in curve]
    if not points:
        return "<p>No equity observations.</p>"
    lo, hi = min(D(p["equity"]) for p in points), max(D(p["equity"]) for p in points)
    start, end = min(p["at_us"] for p in points), max(p["at_us"] for p in points)
    lines = []
    for index, curve in enumerate(curves):
        sampled = curve[:: max(1, len(curve) // 450)]
        if curve and (not sampled or sampled[-1] != curve[-1]):
            sampled.append(curve[-1])
        coordinates = " ".join(
            f"{62 + (p['at_us'] - start) * 880 / max(1, end - start):.1f},"
            f"{220 - float((D(p['equity']) - lo) / max(D('.01'), hi - lo)) * 180:.1f}"
            for p in sampled
        )
        lines.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{COLORS[index]}" stroke-width="2.5"/>'
        )
    legend = " ".join(
        f'<span style="color:{COLORS[i]}">● {esc(LABELS.get(key, key))}</span>'
        for i, key in enumerate(results)
    )
    start_label = datetime.fromtimestamp(start / 1e6, UTC).strftime("%b %d %H:%M UTC")
    end_label = datetime.fromtimestamp(end / 1e6, UTC).strftime("%b %d %H:%M UTC")
    return f"""<div class="legend">{legend}</div><svg viewBox="0 0 1000 270" role="img" aria-label="Simulated account equity over time">
    <line x1="62" y1="220" x2="942" y2="220" stroke="#d7ddd7"/>
    <text x="62" y="24">{money(hi)}</text><text x="62" y="240">{money(lo)}</text>
    {"".join(lines)}<text x="62" y="262">{start_label}</text><text x="942" y="262" text-anchor="end">{end_label}</text></svg>"""


def render(title: str, results: dict, path: Path, *, settings: dict, dataset: dict):
    simple = dataset.get("costs") == "excluded"
    values = {name: metrics(value) for name, value in results.items()}
    heads = "".join(f"<th>{esc(LABELS.get(name, name))}</th>" for name in results)
    rows = []
    for label, key, dollars in [
        ("Simulated result before costs" if simple else "Net simulated result", "net_pnl", True),
        ("Largest account decline", "max_marked_drawdown_usd", True),
        ("Trading fees", "fees", True),
        ("Funding received / paid", "funding", True),
        ("Completed trades", "closed_trades", False),
        ("Positions still open at the end", "open_positions", False),
    ]:
        if simple and key in {"fees", "funding"}:
            continue
        cells = "".join(
            f"<td>{esc(money(v[key]) if dollars else v[key])}</td>" for v in values.values()
        )
        rows.append(f"<tr><th>{esc(label)}</th>{cells}</tr>")
    links = "".join(
        f'<li><a href="{esc(name)}.html">{esc(LABELS.get(name, name))}: detailed report</a> · <a href="{esc(name)}.json">run data</a></li>'
        for name in results
    )
    comparisons = ""
    if len(values) > 1 and not simple:
        first = next(iter(values.values()))
        differences = [
            f"{LABELS.get(k, k)}: {money(D(v['net_pnl']) - D(first['net_pnl']))} relative to the baseline"
            for k, v in list(values.items())[1:]
        ]
        comparisons = (
            "<p>"
            + "; ".join(esc(x) for x in differences)
            + ". This one dataset does not establish a better strategy.</p>"
        )
    trades = []
    for name, result in results.items():
        fills = [e for e in reportable(result)["ledger_journal"] if e["kind"] == "fill"]
        for fill in fills[:200]:
            data = fill["data"]
            time = datetime.fromtimestamp(fill["at_us"] / 1e6, UTC).strftime("%Y-%m-%d %H:%M:%S")
            trades.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(x)}</td>"
                    for x in [
                        LABELS.get(name, name),
                        time,
                        data.get("owner", ""),
                        data.get("symbol", ""),
                        "Exit" if data.get("reduce_only") else "Entry",
                        data.get("quantity", ""),
                        money(data.get("price")),
                        money(data.get("fee", 0)),
                    ]
                )
                + "</tr>"
            )
    assumptions = "".join(
        f"<tr><th>{esc(k.replace('_', ' '))}</th><td>{esc(v)}</td></tr>"
        for k, v in settings.items()
        if k not in {"title", "dataset", "data_description"}
    )
    limitations = list(
        dict.fromkeys(
            str(x) for r in results.values() for x in reportable(r).get("limitations", [])
        )
    )
    limits = "".join(f"<li>{esc(value)}</li>" for value in limitations)
    purpose = (
        "how your rules behave in five fictional market scenarios. Fees, spread, slippage and "
        "funding are excluded. Synthetic results do not establish real-world profitability."
        if simple
        else "how these rules behaved on these inputs after the stated costs. A favorable result here "
        "is not evidence of future profitability. No live account is connected."
    )
    explanation = (
        "Each column applies the same strategy to a different scenario. Results include the "
        "marked value of positions still open, with zero costs. Largest account decline is the "
        "biggest dollar fall from an earlier equity peak."
        if simple
        else "Net result includes fees, modeled funding and marked open positions. “Largest account "
        "decline” is the biggest dollar fall from an earlier equity peak. Slippage is included "
        "in execution prices."
    )
    change = (
        "Change one strategy rule in examples/strategy.json, keep the seed and scenarios fixed, "
        "and run into a new folder. Then try other seeds without selecting only the best result."
        if simple
        else "Edit a policy in your settings file, such as breakeven-1R to breakeven-1.5R, then run "
        "it into a new output folder. Keep the dataset and other settings the same. "
        "1R is the original entry-to-stop distance; moving to entry can still lose after costs."
    )
    document = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Liquid Strategy Lab</title><style>
:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f0;color:#19322c;font:16px/1.6 system-ui,sans-serif}}header{{background:#153d34;color:white;padding:24px max(24px,calc((100vw - 1100px)/2))}}header b{{font-size:20px}}header small{{display:block;color:#cedbd2}}main{{max-width:1150px;margin:36px auto;padding:0 24px}}h1{{font-size:clamp(28px,4vw,42px);line-height:1.2;max-width:900px}}h2{{font-size:23px}}p{{max-width:900px}}.tag{{display:inline-block;background:#f4dfaf;color:#584512;padding:4px 12px;border-radius:20px;font-size:13px;font-weight:700;letter-spacing:.03em}}.card{{background:white;border:1px solid #dde4da;border-radius:12px;padding:24px;margin:24px 0}}.note{{background:#fbf5e5;border-left:4px solid #c08c32;padding:16px 20px}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;text-align:left}}th,td{{padding:12px;border-bottom:1px solid #e3e8e0;vertical-align:top}}thead{{background:#f4f7f1}}tbody th{{font-weight:500}}svg{{width:100%;min-width:480px}}svg text{{font:12px system-ui;fill:#53685d}}.legend{{display:flex;gap:24px;flex-wrap:wrap;font-weight:600}}a{{color:#086c5c}}code{{overflow-wrap:anywhere}}details{{margin:20px 0}}summary{{cursor:pointer;font-weight:600}}footer{{font-size:14px;color:#57695f;padding:24px 0}}.muted{{color:#57695f}}@media(max-width:600px){{.card{{padding:14px}}th,td{{padding:10px}}main{{padding:0 16px}}}}
</style></head><body><header><b>Liquid Strategy Lab</b><small>Local research for Liquid users and the ATG community</small></header><main>
<span class="tag">{esc(dataset["kind"].upper())} DATA · SIMULATED MONEY</span><h1>{esc(title)}</h1>
<p>{esc(dataset["description"])}</p><p class="note"><strong>What this answers:</strong> {esc(purpose)}</p>
<section class="card"><h2>Compare the outcomes</h2><div class="scroll"><table><thead><tr><th>Measure</th>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>{comparisons}<p class="muted">{esc(explanation)}</p></section>
<section class="card"><h2>Account value through the test</h2><div class="scroll">{chart(results)}</div></section>
<section class="card"><h2>Try one change</h2><p>{esc(change)}</p><details><summary>Settings used</summary><div class="scroll"><table>{assumptions}</table></div></details></section>
<section class="card"><h2>Inspect the trades</h2><p>Up to 200 simulated fills per version are shown below. The linked run data retains every decision and fill.</p><details><summary>Show simulated trades</summary><div class="scroll"><table><thead><tr><th>Version</th><th>Time (UTC)</th><th>Agent</th><th>Market</th><th>Action</th><th>Quantity</th><th>Price</th><th>Fee</th></tr></thead><tbody>{"".join(trades) or '<tr><td colspan="8">No simulated fills. Inspect the rejection reasons in the detailed report.</td></tr>'}</tbody></table></div></details></section>
<section class="card"><h2>Evidence and limits</h2><ul>{limits}</ul><details><summary>Download and reproduce</summary><ul>{links}<li><a href="comparison.json">Comparison and settings</a></li></ul><p>Input checksum: <code>{esc(dataset.get("sha256", "authored-event-fixtures"))}</code></p></details></section>
<footer>Independent community software. Not an official Liquid product. All results on this page are simulated.</footer></main></body></html>"""
    path.write_text(document, encoding="utf-8")
    return values
