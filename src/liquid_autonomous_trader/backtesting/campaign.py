"""Bounded, append-only scenario attempts with exact-code resumability."""

from __future__ import annotations

import html
import json
import platform
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import canonical, digest
from liquid_autonomous_trader.backtesting.history import sha256
from liquid_autonomous_trader.backtesting.portfolio import safe_reason
from liquid_autonomous_trader.backtesting.scenarios import Scenario, declaration, freeze
from liquid_autonomous_trader.backtesting.stress_execution import execute as execution_case
from liquid_autonomous_trader.backtesting.stress_jev import execute as jev_case
from liquid_autonomous_trader.backtesting.stress_market import execute as market_case
from liquid_autonomous_trader.backtesting.stress_market import prepare as market_data
from liquid_autonomous_trader.backtesting.stress_portfolio import execute as portfolio_case
from liquid_autonomous_trader.backtesting.stress_source import execute as source_case
from liquid_strategy_lab.portable import dependency_hash, rss_bytes


def package_hash():
    # Adapters execute production code outside this research directory. A source
    # change there must invalidate resume keys just as a simulator change does.
    root = Path(__file__).parents[1]
    return digest({p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*.py"))})


class Attempts:
    def __init__(self, path):
        if path.exists():
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as probe:
                tables = {
                    r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if tables and "research_campaign_v1" not in tables:
                    raise ValueError("not_a_research_campaign")
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS research_campaign_v1(
              attempt INTEGER PRIMARY KEY, case_id TEXT NOT NULL, key TEXT NOT NULL,
              status TEXT NOT NULL, result TEXT NOT NULL, receipt TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS campaign_key ON research_campaign_v1(key,attempt);
            CREATE TRIGGER IF NOT EXISTS campaign_no_update BEFORE UPDATE ON research_campaign_v1
              BEGIN SELECT RAISE(ABORT,'immutable_campaign_attempt'); END;
            CREATE TRIGGER IF NOT EXISTS campaign_no_delete BEFORE DELETE ON research_campaign_v1
              BEGIN SELECT RAISE(ABORT,'immutable_campaign_attempt'); END;
        """)

    def latest(self, key):
        row = self.db.execute(
            "SELECT * FROM research_campaign_v1 WHERE key=? ORDER BY attempt DESC LIMIT 1", (key,)
        ).fetchone()
        return dict(row) if row else None

    def append(self, case, key, result, receipt):
        encoded = canonical(result)
        if len(encoded.encode()) > 8 * 1024**2:
            raise ValueError("scenario_result_size_budget")
        with self.db:
            row = self.db.execute(
                "INSERT INTO research_campaign_v1(case_id,key,status,result,receipt) "
                "VALUES(?,?,?,?,?)",
                (case.case_id, key, result["status"], encoded, canonical(receipt)),
            )
        return row.lastrowid

    def close(self):
        self.db.close()


def run(
    directory: Path,
    *,
    family=None,
    case_ids=None,
    limit=None,
    retry_failed=False,
    market_cache=None,
):
    directory.mkdir(parents=True, exist_ok=True)
    frozen = declaration()
    freeze(directory / "declaration.json")
    if family is not None and family not in frozen["targets"]:
        raise ValueError("unknown_campaign_family")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("positive_case_limit_required")
    declared = [row for row in frozen["cases"] if family is None or row["family"] == family]
    if case_ids:
        if not set(case_ids) <= {row["case_id"] for row in declared}:
            raise ValueError("case_not_in_selected_declaration")
        declared = [row for row in declared if row["case_id"] in case_ids]
    code_hash = package_hash()
    dependencies = dependency_hash()
    catalog = Attempts(directory / "attempts.sqlite")
    begin, cpu = time.monotonic(), time.process_time()
    prior_seconds = sum(
        json.loads(row[0])["wall_seconds"]
        for row in catalog.db.execute("SELECT receipt FROM research_campaign_v1")
    )
    selected, executed, reused = [], 0, 0
    stopped = None
    data_binding = None
    if any(row["family"] == "market" for row in declared):
        if market_cache is None:
            catalog.close()
            raise ValueError("verified_market_cache_required")
        data_binding = market_data(market_cache)[3]
    dispatch = {
        "execution": execution_case,
        "jev": jev_case,
        "source": source_case,
        "portfolio": portfolio_case,
        "market": lambda case: market_case(case, root=market_cache),
    }
    try:
        for row in declared:
            case = Scenario(**row)
            key = digest(
                [
                    row,
                    code_hash,
                    dependencies,
                    frozen["declaration_sha256"],
                    data_binding if case.family == "market" else None,
                ]
            )
            previous = catalog.latest(key)
            if previous and (previous["status"] == "passed" or not retry_failed):
                selected.append(
                    {
                        "case_id": case.case_id,
                        "family": case.family,
                        "attempt": previous["attempt"],
                        "status": previous["status"],
                        "core_hash": json.loads(previous["result"]).get("core_hash"),
                    }
                )
                reused += 1
                continue
            if limit is not None and executed >= limit:
                stopped = "invocation_case_limit"
                break
            used_disk = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
            budget = frozen["resources"]
            if (
                prior_seconds + time.monotonic() - begin > budget["wall_seconds"]
                or rss_bytes() > budget["rss_bytes"]
                or used_disk + 8 * 1024**2 > budget["output_bytes"]
            ):
                stopped = "declared_resource_budget"
                break
            started, started_cpu = time.monotonic(), time.process_time()
            try:
                if case.family not in dispatch:
                    raise ValueError("scenario_handler_not_implemented")
                result = dispatch[case.family](case)
            except Exception as error:
                result = {
                    "case_id": case.case_id,
                    "status": "failed",
                    "error": type(error).__name__,
                    "reason": safe_reason(error),
                    "configuration": row,
                }
            receipt = {
                "configuration": row,
                "configuration_sha256": digest(row),
                "research_code_sha256": code_hash,
                "dependency_lock_sha256": dependencies,
                "declaration_sha256": frozen["declaration_sha256"],
                "wall_seconds": time.monotonic() - started,
                "cpu_seconds": time.process_time() - started_cpu,
                "observed_rss_bytes": rss_bytes(),
                "recorded_at": datetime.now(UTC).isoformat(),
                "machine": platform.platform(),
                "inference_spend_usd": "0",
            }
            attempt = catalog.append(case, key, result, receipt)
            selected.append(
                {
                    "case_id": case.case_id,
                    "family": case.family,
                    "attempt": attempt,
                    "status": result["status"],
                    "core_hash": result.get("core_hash"),
                }
            )
            executed += 1
        counts = dict(Counter(row["status"] for row in selected))
        complete = len(selected) == len(declared) and counts.get("passed", 0) == len(declared)
        all_cases = len(declared) == 500 and complete
        core = {
            "schema": "liquid-scenario-campaign-v1",
            "cases": selected,
            "selected_family": family,
            "declared_selected_cases": len(declared),
            "total_declared_cases": 500,
            "counts": counts,
            "selected_cases_passed": complete,
            "acceptance_campaign_complete": all_cases,
            "declaration_sha256": frozen["declaration_sha256"],
            "research_code_sha256": code_hash,
            "dependency_lock_sha256": dependencies,
            "market_data_binding": data_binding,
            "evidence": "conditional synthetic behavior; independent historical datasets=0",
            "verdict": "unsupported_for_strategy_improvement",
        }
        result = {
            **core,
            "core_hash": digest(core),
            "invocation": {
                "executed": executed,
                "reused_exact_attempts": reused,
                "stopped": stopped,
                "wall_seconds": time.monotonic() - begin,
                "cpu_seconds": time.process_time() - cpu,
                "observed_rss_bytes": rss_bytes(),
                "machine": platform.platform(),
                "output_bytes": sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()),
            },
        }
        name = family or "all"
        (directory / (name + "-summary.json")).write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n"
        )
        render(result, directory / (name + "-summary.html"))
        return result
    finally:
        catalog.close()


def render(result, path):
    rows = "".join(
        "<tr>"
        + "".join(
            "<td>" + html.escape(str(row.get(k))) + "</td>"
            for k in ("case_id", "status", "attempt", "core_hash")
        )
        + "</tr>"
        for row in result["cases"]
    )
    path.write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        "<title>Liquid scenario acceptance</title><style>"
        "body{font:16px system-ui;max-width:1200px;margin:40px auto}"
        "td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd;overflow-wrap:anywhere}"
        "</style><h1>Liquid scenario acceptance</h1><p>Conditional behavior tests. "
        "No profitability or live promotion claim. All attempts remain in the local journal.</p>"
        "<p>" + html.escape(str(result["counts"])) + "</p><table><tr><th>Case</th><th>Status</th>"
        "<th>Attempt</th><th>Core hash</th></tr>" + rows + "</table></html>"
    )
