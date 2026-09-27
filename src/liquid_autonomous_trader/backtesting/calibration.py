"""Chronological fee calibration from sanitized, account/route-bound native fills.

This offline module cannot fetch account data. Fee is already the complete native
charge; builderFee is a component, never an additional cost. Order groups stay on
one side of the chronological boundary, including fills crossing that boundary.
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import digest, require_sanitized


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def calibrate(evidence: dict, *, split_ms: int) -> dict:
    require_sanitized(evidence)
    if not evidence.get("identity_matches_runtime_pin") or not evidence.get(
        "operation_route_verified"
    ):
        raise ValueError("account_route_binding_required")
    if evidence.get("route") != "live" or type(split_ms) is not int or split_ms <= 0:
        raise ValueError("explicit_route_and_chronological_split_required")
    rows, excluded, orders = [], Counter(), defaultdict(list)
    seen = {}
    for raw in evidence["fills"]:
        identity = raw["fill_id"]
        if identity in seen:
            if seen[identity] != digest(raw):
                raise ValueError("conflicting_native_fill")
            continue
        seen[identity] = digest(raw)
        if raw.get("attribution") != "exact_order_symbol_route":
            excluded["unmatched_order_symbol_route"] += 1
            continue
        if raw.get("feeToken") != "USDC" or type(raw.get("crossed")) is not bool:
            excluded["fee_currency_or_liquidity_role_unknown"] += 1
            continue
        quantity, price, fee = (D(raw[k]) for k in ("sz", "px", "fee"))
        if not all(x.is_finite() for x in (quantity, price, fee)) or min(quantity, price) <= 0:
            raise ValueError("invalid_native_economic_fill")
        if type(raw["time"]) is not int or raw["time"] <= 0:
            raise ValueError("invalid_native_fill_time")
        row = dict(raw, notional=quantity * price, fee_value=fee)
        rows.append(row)
        orders[row["order_id"]].append(row)
    straddling = {
        key
        for key, items in orders.items()
        if min(r["time"] for r in items) < split_ms <= max(r["time"] for r in items)
    }
    groups = defaultdict(list)
    for row in rows:
        if row["order_id"] in straddling:
            excluded["order_crosses_split"] += 1
            continue
        # Separate native market, maker/taker and operation kind. Never infer exit
        # costs from entries or pool routes whose discounts may differ.
        key = (row["coin"], "taker" if row["crossed"] else "maker", row["operation_kind"])
        groups[key].append(row)
    results = []
    for (coin, liquidity, kind), items in sorted(groups.items()):
        training = [r for r in items if r["time"] < split_ms]
        validation = [r for r in items if r["time"] >= split_ms]
        notional = sum((r["notional"] for r in training), D(0))
        fee = sum((r["fee_value"] for r in training), D(0))
        rate = fee / notional if notional else None
        residuals = []
        for row in validation:
            predicted = row["notional"] * rate if rate is not None else None
            residuals.append(
                {
                    "fill_id": row["fill_id"],
                    "time_ms": row["time"],
                    "actual_fee": str(row["fee_value"]),
                    "predicted_fee": str(predicted) if predicted is not None else None,
                    "residual_usd": str(row["fee_value"] - predicted)
                    if predicted is not None
                    else None,
                }
            )
        known = [D(r["residual_usd"]) for r in residuals if r["residual_usd"] is not None]
        results.append(
            {
                "instrument": coin,
                "liquidity": liquidity,
                "operation_kind": kind,
                "first_fill": iso(min(r["time"] for r in items)),
                "last_fill": iso(max(r["time"] for r in items)),
                "training_fills": len(training),
                "training_orders": len({r["order_id"] for r in training}),
                "validation_fills": len(validation),
                "validation_orders": len({r["order_id"] for r in validation}),
                "training_notional_usd": str(notional),
                "training_fees_usd": str(fee),
                "estimated_fee_rate": str(rate) if rate is not None else None,
                "validation_residual_mean_usd": str(sum(known, D(0)) / len(known))
                if known
                else None,
                "validation_residual_max_abs_usd": str(max(map(abs, known))) if known else None,
                "validation_residuals": residuals,
                "status": "descriptive_validation" if known else "insufficient_validation",
            }
        )
    lag = []
    for row in rows:
        if row.get("operation_started_at"):
            started = datetime.fromisoformat(row["operation_started_at"])
            if started.tzinfo is None:
                raise ValueError("aware_operation_time_required")
            lag.append(row["time"] - int(started.timestamp() * 1000))
    result = {
        "schema": "liquid-native-fee-calibration-v1",
        "evidence_sha256": digest(evidence),
        "account": "research-account-1",
        "route": evidence["route"],
        "observed_at": evidence["observed_at"],
        "split_utc": iso(split_ms),
        "split_rule": "native fill time; purge entire order when partial fills straddle split",
        "retained_unique_fills": len(seen),
        "matched_economic_fills": len(rows),
        "excluded": dict(excluded),
        "groups": results,
        "local_start_to_native_fill_ms": {
            "count": len(lag),
            "negative_count": sum(v < 0 for v in lag),
            "minimum": min(lag) if lag else None,
            "maximum": max(lag) if lag else None,
            "status": "descriptive_clock_dependent_not_transport_latency",
        },
        "limitations": [
            "fee includes builder component; component is not added again",
            "entry observations do not identify exit or maker fees",
            "estimated rates apply only to observed route, market, liquidity role and dates",
            "not enough independent observations to certify stationary future fee schedules",
            "historical fees are not automatically the fee for an earlier proxy backtest",
            "no synchronized pre-submit book: impact, queue, adverse selection "
            "and latency drift unsupported",
            "local request-start and native fill clocks are not calibrated transport timestamps",
        ],
        "verdict": "partial_fee_calibration_only",
    }
    result["core_sha256"] = digest(result)
    return result


def render_calibration(result, path: Path):
    sections = []
    for group in result["groups"]:
        values = [
            float(r["residual_usd"])
            for r in group["validation_residuals"]
            if r["residual_usd"] is not None
        ]
        scale = max(map(abs, values), default=0) or 1
        circles = "".join(
            f'<circle cx="{20 + i * 560 / max(1, len(values) - 1):.2f}" '
            f'cy="{80 - value / scale * 60:.2f}" r="3"/>'
            for i, value in enumerate(values)
        )
        label = html.escape(
            " / ".join(group[k] for k in ("instrument", "liquidity", "operation_kind"))
        )
        sections.append(
            f"<section><h2>{label}</h2><p>Training fills: {group['training_fills']}; "
            f"validation fills: {group['validation_fills']}; estimated fee per notional: "
            f"{group['estimated_fee_rate']}. Residual max absolute USD: "
            f"{group['validation_residual_max_abs_usd']}.</p>"
            '<svg viewBox="0 0 600 160" role="img" '
            'aria-label="Validation fee residuals in chronological order">'
            '<path d="M20 80H580" stroke="#888"/><g fill="#185f92">'
            + circles
            + f"</g></svg><p>Zero at center; chart limits ±${scale:.8g}. "
            "Each dot is one validation fill; partial fills are dependent.</p></section>"
        )
    text = (
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Native fee calibration</title>"
    )
    text += (
        "<style>body{font:16px system-ui;max-width:960px;margin:40px auto;padding:0 20px}"
        "svg{max-width:600px;width:100%}</style>"
    )
    text += (
        f"<h1>Native fee calibration</h1><p>{result['verdict']}; split {result['split_utc']}.</p>"
    )
    text += "".join(sections) + "<h2>Coverage limits</h2><ul>"
    text += "".join(f"<li>{html.escape(x)}</li>" for x in result["limitations"])
    path.write_text(text + "</ul></html>", encoding="utf-8")
