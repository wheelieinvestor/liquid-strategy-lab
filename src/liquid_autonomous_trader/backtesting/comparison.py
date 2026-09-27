"""Frozen, resumable policy study. Missing evidence cannot authorize promotion."""

from __future__ import annotations

import html
import json
import platform
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from liquid_autonomous_trader.backtesting.campaign import Attempts, package_hash, rss_bytes
from liquid_autonomous_trader.backtesting.events import Catalog, canonical, digest
from liquid_autonomous_trader.backtesting.historical import MINUTE_US, HistoryAssumptions, run_btc
from liquid_autonomous_trader.backtesting.history import sha256
from liquid_autonomous_trader.backtesting.portfolio import safe_reason
from liquid_autonomous_trader.backtesting.report import metrics, render, trade_cohorts
from liquid_autonomous_trader.backtesting.statistics import (
    WEEK_US,
    holm,
    paired_uncertainty,
    verdict,
    weekly_pairs,
)
from liquid_strategy_lab.portable import dependency_hash, source_hash

POLICIES = (
    "current-v5-missing-cache-hold",
    "legacy-production",
    "breakeven-1R",
    "breakeven-1.25R",
    "breakeven-1.5R",
)
MAX_RESULT_BYTES = 64 * 1024**2


def stamp(year, month, day=1):
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1_000_000)


def declaration():
    """No validation-driven search. First week is already exposed development."""
    folds = []
    for year, month in [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 5)]:
        start = datetime(year, month, 1, tzinfo=UTC) + timedelta(days=7)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
        folds.append(
            {
                "id": f"{year}-{month:02d}",
                "split": "development" if year == 2025 else "validation",
                "start_us": int(start.timestamp() * 1_000_000),
                "end_us": int(end.timestamp() * 1_000_000),
            }
        )
    costs = [
        {"id": "base", "changes": {}},
        {"id": "fee-95", "changes": {"fee_per_side": "0.00095"}},
        {"id": "fee-120", "changes": {"fee_per_side": "0.0012"}},
        {"id": "spread-3", "changes": {"spread_bps": "3"}},
        {"id": "spread-8", "changes": {"spread_bps": "8"}},
        {"id": "slip-2", "changes": {"extra_slippage_bps": "2"}},
        {"id": "slip-5", "changes": {"extra_slippage_bps": "5"}},
        {"id": "favorable", "changes": {"adverse_intrabar": False}},
        {
            "id": "combined-adverse",
            "changes": {"fee_per_side": "0.0012", "spread_bps": "8", "extra_slippage_bps": "5"},
        },
    ]
    return {
        "schema": "liquid-comparison-plan-v1",
        "policies": list(POLICIES),
        "assumptions": asdict(HistoryAssumptions()),
        "folds": folds,
        "sensitivity": costs,
        "matched_window": [stamp(2025, 7, 2), stamp(2025, 7, 9)],
        "matched_selection": "all complete single-fill baseline entries, at most 16; no outcomes",
        "resources": {"wall_seconds": 1800, "rss_bytes": 2 * 1024**3, "output_bytes": 4 * 1024**3},
        "workers": 1,
        "data_spend_usd": "0",
        "inference_spend_usd": "0",
        "statistics": {
            "family_trials": 5,
            "samples": 4000,
            "seed": 20260925,
            "weekly_moving_block_length": 2,
            "minimum_effective_spans": 12,
            "reason_for_two_week_dependence_span": "a trade crossing a weekly mark "
            "correlates adjacent weekly deltas; "
            "holdings exceeding two weeks additionally invalidate sufficiency",
            "correction": "Holm across five policy hypotheses including missing/failed; "
            "Bonferroni simultaneous bootstrap bounds; net PnL, not Sharpe optimization",
        },
        "interpretation": [
            "One frozen grid; no candidate selected for final testing or live activation",
            "Monthly folds restart flat with $1000; marked endpoints, no compounded return claim",
            "Seven-day purge before each fold; only past 24h used for feature warmup",
            "Partial weeks contribute net economics but not uncertainty samples",
            "Resampling respects monthly reset boundaries and split boundaries",
            "Sensitivity is one-factor plus joint adverse, not an unreported search",
            "Four-agent comparison is a synthetic behavior fixture; "
            "native historical PnL unsupported",
            "May-June reserve already exposed; no untouched final test certified or opened",
            "Missing exact historical model/cache, books, oracle and margin means unsupported",
        ],
    }


def matched_cohorts(baseline):
    decisions = {r["signal"]["signal_id"]: r for r in baseline["decisions"] if "signal" in r}
    cohorts, excluded = [], []
    for order in baseline["execution"]["orders"].values():
        fills = [
            row
            for row in baseline["ledger_journal"]
            if row["kind"] == "fill" and row["data"].get("order_id") == order["order_id"]
        ]
        if len(fills) != 1 or D(fills[0]["data"]["quantity"]) != D(order["signed_quantity"]):
            excluded.append({"id": order["order_id"], "reason": "not_one_complete_starting_fill"})
            continue
        decision = decisions[order["order_id"]]
        cohorts.append(
            {
                "order": order,
                "fill": fills[0],
                "plan": {
                    "target": decision["signal"]["bracket"]["target_price"],
                    "tick_size": baseline["assumptions"]["native_price_step"],
                },
            }
        )
    cohorts.sort(key=lambda r: (r["fill"]["at_us"], r["order"]["order_id"]))
    if len(cohorts) > 16:
        raise ValueError("matched_cohort_budget_exceeded_no_outcome_filtering")
    return cohorts, excluded


def portfolio_fixture(policy):
    from liquid_autonomous_trader.backtesting.fixtures import adapters, portfolio_events
    from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine

    source = adapters()
    sim = source.account.sim
    source.close()
    sim.ledger.instruments["ETH"] = replace(sim.ledger.instruments["BTC"], symbol="ETH")
    engine = PortfolioEngine(
        sim,
        policy="current-v5-exact" if policy == POLICIES[0] else policy,
        mode="synthetic_stress",
    )
    try:
        before, following = portfolio_events()
        return engine.run([*before, *following])
    finally:
        engine.close()


def source_binding():
    root = Path(__file__).parents[1]
    return digest({p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*.py"))})


def run(directory, cache, *, limit=None, retry_failed=False):
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("positive_task_limit_required")
    directory.mkdir(parents=True, exist_ok=True)
    plan = declaration()
    plan_path = directory / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
        raise ValueError("frozen_study_plan_mismatch")
    if not plan_path.exists():
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    binding = {
        "package_sha256": package_hash(),
        "all_production_source_sha256": source_binding(),
        "dependency_lock_sha256": dependency_hash(),
        "data_manifest_sha256": sha256(cache / "manifest.json"),
        "data_file_sha256": sha256(cache / "dataset.json"),
        "plan_sha256": digest(plan),
        "source_sha": source_hash(),
        "baseline_source_sha": "e475636f4dc6acbc3af4fa6f9520dc0dd8e02f34",
    }
    # Git HEAD changes on commit without changing code. Resume keys bind actual
    # source bytes; receipts separately retain the observed commit provenance.
    key_binding = {k: v for k, v in binding.items() if k != "source_sha"}
    attempts = Attempts(directory / "attempts.sqlite")
    exposures = Catalog(directory / "exposures.sqlite")
    begin, cpu = time.monotonic(), time.process_time()
    used_time = sum(
        json.loads(r[0])["wall_seconds"]
        for r in attempts.db.execute("SELECT receipt FROM research_campaign_v1")
    )
    selected = []
    executed, reused = 0, 0

    def guard():
        if (
            used_time + time.monotonic() - begin > plan["resources"]["wall_seconds"]
            or rss_bytes() > plan["resources"]["rss_bytes"]
        ):
            raise ValueError("declared_study_resource_budget")

    def task(spec, factory):
        nonlocal executed, reused
        key = digest([key_binding, spec])
        previous = attempts.latest(key)
        if (
            previous
            and previous["status"] != "started"
            and (previous["status"] == "passed" or not retry_failed)
        ):
            result = json.loads(previous["result"])
            if result.get("artifact"):
                path = directory / result["artifact"]
                if not path.is_file() or sha256(path) != result["artifact_sha256"]:
                    raise ValueError("resumed_study_artifact_corrupted")
            selected.append(result)
            reused += 1
            return result
        if limit is not None and executed >= limit:
            raise StopIteration("invocation_task_limit")
        guard()
        used_disk = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
        if used_disk + MAX_RESULT_BYTES * 2 > plan["resources"]["output_bytes"]:
            raise ValueError("declared_study_disk_budget")
        started, started_cpu = time.monotonic(), time.process_time()
        # The start row survives interruption, preserving incomplete attempts.
        case = SimpleNamespace(case_id=spec["id"])
        attempt = attempts.append(
            case,
            key,
            {"status": "started", "configuration": spec},
            {"wall_seconds": 0, "binding": binding},
        )
        try:
            if "start_us" in spec:
                exposures.expose(key, "frozen_policy_comparison", spec["start_us"], spec["end_us"])
            run_result = factory()
            guard()
            encoded = canonical(run_result)
            if len(encoded.encode()) > MAX_RESULT_BYTES:
                raise ValueError("study_run_artifact_size_budget")
            name = f"run-{key[:20]}-{attempt}.json"
            path = directory / name
            with path.open("x") as stream:
                stream.write(encoded + "\n")
            values = metrics(run_result)
            render(run_result, path.with_suffix(".html"))
            result = {
                "status": "passed",
                "configuration": spec,
                "artifact": name,
                "artifact_sha256": sha256(path),
                "core_hash": run_result.get("core_hash", run_result.get("core_sha256")),
                "metrics": values,
            }
            if spec["kind"] == "fold":
                closed, opened = trade_cohorts(run_result["ledger_journal"])
                durations = [t["exit_us"] - t["entry_us"] for t in closed]
                durations += [spec["end_us"] - t["entry_us"] for t in opened]
                result["holds_within_dependence_span"] = max(durations, default=0) <= 2 * WEEK_US
        except Exception as error:
            result = {
                "status": "failed",
                "configuration": spec,
                "error": type(error).__name__,
                "reason": safe_reason(error),
            }
        receipt = {
            "binding": binding,
            "wall_seconds": time.monotonic() - started,
            "cpu_seconds": time.process_time() - started_cpu,
            "peak_rss_bytes": rss_bytes(),
            "machine": platform.platform(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "inference_spend_usd": "0",
            "data_spend_usd": "0",
        }
        attempts.append(case, key, result, receipt)
        selected.append(result)
        executed += 1
        print(
            json.dumps(
                {
                    "task": spec["id"],
                    "status": result["status"],
                    "net_pnl": result.get("metrics", {}).get("net_pnl"),
                }
            ),
            flush=True,
        )
        return result

    stopped = None
    try:
        first, last = plan["matched_window"]
        baseline = None
        for cost in plan["sensitivity"]:
            for policy in POLICIES:
                spec = {
                    "id": f"sensitivity/{cost['id']}/{policy}",
                    "kind": "sensitivity",
                    "cost": cost["id"],
                    "policy": policy,
                    "start_us": first,
                    "end_us": last,
                }
                settings = replace(HistoryAssumptions(), policy=policy, **cost["changes"])
                row = task(
                    spec, lambda s=settings: run_btc(cache, first, last, s, resource_guard=guard)
                )
                if cost["id"] == "base" and policy == POLICIES[0] and row["status"] == "passed":
                    baseline = json.loads((directory / row["artifact"]).read_text(encoding="utf-8"))
        if baseline is not None:
            cohorts, exclusions = matched_cohorts(baseline)
            frozen_cohorts = {
                "cohorts": cohorts,
                "exclusions": exclusions,
                "baseline_core_hash": baseline["core_hash"],
            }
            cohort_path = directory / ("cohorts-" + digest(frozen_cohorts)[:20] + ".json")
            if not cohort_path.exists():
                cohort_path.write_text(canonical(frozen_cohorts) + "\n", encoding="utf-8")
            for i, cohort in enumerate(cohorts):
                start = cohort["fill"]["at_us"] // (15 * MINUTE_US) * 15 * MINUTE_US
                for policy in POLICIES:
                    spec = {
                        "id": f"matched/{i}/{policy}",
                        "kind": "matched",
                        "cohort": i,
                        "policy": policy,
                        "cohort_sha256": digest(cohort),
                        "start_us": start,
                        "end_us": last,
                    }
                    task(
                        spec,
                        lambda c=cohort, p=policy, s=start: run_btc(
                            cache,
                            s,
                            last,
                            replace(HistoryAssumptions(), policy=p),
                            matched_entry=c,
                            resource_guard=guard,
                        ),
                    )
        for fold in plan["folds"]:
            for policy in POLICIES:
                spec = {
                    "id": f"fold/{fold['id']}/{policy}",
                    "kind": "fold",
                    "policy": policy,
                    "fold": fold["id"],
                    "split": fold["split"],
                    "start_us": fold["start_us"],
                    "end_us": fold["end_us"],
                }
                task(
                    spec,
                    lambda f=fold, p=policy: run_btc(
                        cache,
                        f["start_us"],
                        f["end_us"],
                        replace(HistoryAssumptions(), policy=p),
                        resource_guard=guard,
                    ),
                )
        for policy in POLICIES:
            task(
                {"id": "portfolio/" + policy, "kind": "portfolio", "policy": policy},
                lambda p=policy: portfolio_fixture(p),
            )
    except StopIteration as error:
        stopped = str(error)
    except ValueError as error:
        stopped = safe_reason(error)
    finally:
        exposures.close()
        attempts.close()
    result = summarize(selected, directory, plan)
    core = {
        "schema": "liquid-comparison-v1",
        "binding": key_binding,
        "plan": plan,
        "runs": selected,
        **result,
        "stopped": stopped,
    }
    summary = {
        **core,
        "core_hash": digest(core),
        "invocation": {
            "executed": executed,
            "reused": reused,
            "wall_seconds": time.monotonic() - begin,
            "cpu_seconds": time.process_time() - cpu,
            "peak_rss_bytes": rss_bytes(),
            "machine": platform.platform(),
            "source_sha": binding["source_sha"],
            "output_bytes": sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()),
        },
    }
    (directory / "comparison.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    render_comparison(summary, directory / "comparison.html")
    return summary


def summarize(rows, directory, plan):
    groups = []
    successful = [r for r in rows if r["status"] == "passed"]
    for split in ("development", "validation"):
        comparisons = {}
        for policy in POLICIES[1:]:
            blocks, tails, deltas, spans_ok, failed = [], D(0), D(0), True, []
            for fold in [f for f in plan["folds"] if f["split"] == split]:
                by_policy = {
                    r["configuration"]["policy"]: r
                    for r in successful
                    if r["configuration"].get("fold") == fold["id"]
                }
                if policy not in by_policy or POLICIES[0] not in by_policy:
                    failed.append(fold["id"])
                    continue
                left, right = by_policy[POLICIES[0]], by_policy[policy]
                a = json.loads((directory / left["artifact"]).read_text(encoding="utf-8"))
                b = json.loads((directory / right["artifact"]).read_text(encoding="utf-8"))
                paired = weekly_pairs(a, b)
                if paired["status"] != "paired":
                    failed.append(fold["id"])
                    continue
                values = [r["net_delta"] for r in paired["blocks"]]
                # Pair neighboring weeks within a fold. Independent folds are
                # never artificially joined into a synthetic two-week block.
                blocks.extend(
                    str(D(values[i]) + D(values[i + 1])) for i in range(0, len(values) - 1, 2)
                )
                tails += D(paired["partial_tail_delta"])
                if len(values) % 2:
                    tails += D(values[-1])
                deltas += D(right["metrics"]["net_pnl"]) - D(left["metrics"]["net_pnl"])
                spans_ok &= left["holds_within_dependence_span"]
                spans_ok &= right["holds_within_dependence_span"]
            interval = paired_uncertainty(blocks, seed=20260925, moving_blocks=1)
            interval["unit"] = "nonoverlapping paired 14-day spans within monthly folds"
            interval["method"] = (
                "bootstrap of paired 14-day spans; monthly reset boundaries retained"
            )
            interval["mean_14day_delta"] = interval.pop("mean_weekly_delta")
            # verdict operates on generic interval arithmetic despite the display unit.
            check = {**interval, "mean_weekly_delta": interval["mean_14day_delta"]}
            comparisons[policy] = {
                "interval": interval,
                "check": check,
                "net_delta": str(deltas),
                "unbootstrapped_tail_delta": str(tails),
                "failed_or_unpaired_folds": failed,
                "holds_within_span": spans_ok,
            }
        corrected = holm({k: v["interval"]["p_one_sided"] for k, v in comparisons.items()})
        neighbor_deltas = [D(comparisons[p]["net_delta"]) for p in POLICIES[2:]]
        stable = all(v > 0 for v in neighbor_deltas) or all(v < 0 for v in neighbor_deltas)
        for policy, comparison in comparisons.items():
            label, reasons = verdict(
                comparison.pop("check"),
                adjusted_p=corrected[policy],
                coverage_complete=False,
                invariants_passed=not comparison["failed_or_unpaired_folds"],
                neighboring_signs_stable=stable,
                holds_within_dependence_span=comparison["holds_within_span"],
            )
            groups.append(
                {
                    "split": split,
                    "policy": policy,
                    **comparison,
                    "holm_adjusted_p": corrected[policy],
                    "neighboring_signs_stable": stable,
                    "verdict": label,
                    "reasons": reasons,
                }
            )
    return {
        "comparisons": groups,
        "counts": {"passed": len(successful), "failed": sum(r["status"] == "failed" for r in rows)},
        "verdict": "unsupported",
        "recommendation": "Retain active policy; no live change supported",
        "limitations": plan["interpretation"],
    }


def render_comparison(result, path):
    def esc(value):
        return html.escape(str(value))

    tables = []
    for kind in ("sensitivity", "matched", "fold", "portfolio"):
        lines = []
        for row in result["runs"]:
            if row["configuration"]["kind"] != kind:
                continue
            m = row.get("metrics", {})
            link = (
                f'<a href="{esc(row["artifact"].replace(".json", ".html"))}">Details</a>'
                if row.get("artifact")
                else esc(row.get("reason", "incomplete"))
            )
            lines.append(
                "<tr>"
                + "".join(
                    f"<td>{esc(x)}</td>"
                    for x in (
                        row["configuration"]["id"],
                        row["status"],
                        m.get("net_pnl"),
                        m.get("fees"),
                        m.get("funding"),
                        m.get("closed_trades"),
                        m.get("open_positions"),
                    )
                )
                + "<td>"
                + link
                + "</td></tr>"
            )
        tables.append(
            f"<h2>{esc(kind)}</h2><table><tr><th>Run</th><th>Status</th>"
            "<th>Net $</th><th>Fees $</th><th>Funding $</th><th>Closed</th>"
            "<th>Open</th><th>Evidence</th></tr>" + "".join(lines) + "</table>"
        )
    comparisons = "".join(
        "<tr>"
        + "".join(
            f"<td>{esc(v)}</td>"
            for v in (
                r["split"],
                r["policy"],
                r["net_delta"],
                r["interval"]["time_blocks"],
                r["interval"]["ci95"],
                r["holm_adjusted_p"],
                r["verdict"],
            )
        )
        + "</tr>"
        for r in result["comparisons"]
    )
    limits = "".join(f"<li>{esc(v)}</li>" for v in result["limitations"])
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><title>Liquid policy comparison</title>'
        "<style>body{font:15px system-ui;margin:32px auto;max-width:1300px;padding:0 20px;"
        "color:#182b33;background:#f5f7f5}table{border-collapse:collapse;width:100%;background:white}"
        "th,td{text-align:left;padding:8px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}"
        ".status{padding:16px;background:#fff0d6}code{overflow-wrap:anywhere}</style>"
        '<h1>Liquid policy comparison</h1><p class="status"><strong>UNSUPPORTED for live '
        "strategy improvement.</strong> " + esc(result["recommendation"]) + "</p>"
        "<p>Current baseline is the documented missing-model fallback, not a historical Jev "
        "decision replay. Costs and margin are conditional assumptions. No inference spend.</p>"
        "<h2>Chronological paired deltas</h2><p>Net dollars sum independent flat-start monthly "
        "folds. Intervals describe mean 14-day paired deltas, not total returns. Incomplete "
        "tails remain in net totals. Five-trial Holm correction; no untouched test opened.</p>"
        "<table><tr><th>Split</th><th>Policy</th><th>Total delta $</th><th>14-day spans</th>"
        "<th>Mean span 95% interval</th><th>Adjusted p</th><th>Verdict</th></tr>"
        + comparisons
        + "</table><h2>Evidence limits</h2><ul>"
        + limits
        + "</ul>"
        + "".join(tables)
        + "<p>Core SHA256: <code>"
        + esc(result["core_hash"])
        + "</code></p></html>",
        encoding="utf-8",
    )
