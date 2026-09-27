"""Exact cached Jev requests and production v5 acceptance, without model IO.

The production worker builds the request; the production acceptance method gates
its actions into an in-memory command collector. No lifecycle transport is exposed.
"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from liquid_autonomous_trader import jev_stop_policy as policy
from liquid_autonomous_trader.backtesting.admission import utc
from liquid_autonomous_trader.backtesting.events import canonical, digest, require_sanitized
from liquid_autonomous_trader.backtesting.ledger import serial
from liquid_autonomous_trader.jev_exit_manager import JevExitManager

FEATURE_VERSION = "liquid-exit-market-v1"
FORBIDDEN = {
    "expected_action",
    "expected_stop_direction",
    "acceptable_stop_prices",
    "labels",
    "future_prices",
    "future_events",
    "realized_outcome",
    "realized_pnl",
}


def validate_model_state(value, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN:
                raise ValueError("model_state_contains_future_or_labels")
            validate_model_state(item, (*path, key))
    elif isinstance(value, list):
        for item in value:
            validate_model_state(item, path)


@dataclass(frozen=True)
class ModelRequest:
    state_json: str
    policy_hash: str = policy.POLICY_HASH
    model: str = policy.MODEL
    feature_version: str = FEATURE_VERSION

    def __post_init__(self):
        state = json.loads(self.state_json)
        validate_model_state(state)
        require_sanitized(state)
        if canonical(state) != self.state_json or len(self.state_json.encode()) > 24000:
            raise ValueError("request_not_canonical_or_too_large")
        if state.get("model") != self.model or state.get("schema_version") != policy.VERSION:
            raise ValueError("request_model_or_schema_mismatch")
        stamp = datetime.fromisoformat(state["as_of"])
        if stamp.tzinfo is None:
            raise ValueError("aware_request_time_required")
        market = state.get("market", {})
        if (
            market.get("bar_end_ms") is not None
            and market["bar_end_ms"] >= stamp.timestamp() * 1000
        ):
            raise ValueError("future_model_bar")
        if (
            market.get("observed_at") is not None
            and datetime.fromisoformat(market["observed_at"]) > stamp
        ):
            raise ValueError("future_model_quote")

    @property
    def state(self):
        return json.loads(self.state_json)

    @property
    def key(self):
        return digest([self.state_json, self.policy_hash, self.model, self.feature_version])


def build_request(base: dict, market: dict) -> ModelRequest:
    """Execute unmodified production feature/candidate/rubric construction.

    `_work` only sees a supplied market reader and a capture callback. The callback
    receives the complete state instead of invoking inference. Worker wall time,
    cost estimates and synthetic response are discarded, never called real usage.
    """
    captured = []
    validate_model_state(base)
    validate_model_state(market)
    worker = object.__new__(JevExitManager)
    worker.policy, worker.stop_focused = policy, True
    worker.market = lambda _: deepcopy(market)
    worker.api_key = ""

    def capture(state, _):
        captured.append(deepcopy(state))
        return {"model": policy.MODEL, "answers": {}}

    worker.ask = capture
    result = worker._work(deepcopy(base))
    if not captured:
        raise ValueError("production_request_inputs_unavailable")
    if result["error"] is not None:
        raise ValueError("production_request_build_failed")
    return ModelRequest(canonical(captured[0]))


class ModelCache:
    def __init__(self, path: Path):
        if path.exists():
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as probe:
                tables = {
                    r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            if tables and "research_model_cache_v1" not in tables:
                raise ValueError("not_a_research_model_cache")
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS research_model_cache_v1 (
                key TEXT PRIMARY KEY, request TEXT NOT NULL,
                record TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS model_no_update BEFORE UPDATE ON research_model_cache_v1
              BEGIN SELECT RAISE(ABORT,'immutable_model_response'); END;
            CREATE TRIGGER IF NOT EXISTS model_no_delete BEFORE DELETE ON research_model_cache_v1
              BEGIN SELECT RAISE(ABORT,'immutable_model_response'); END;
        """)

    def put(
        self,
        request: ModelRequest,
        *,
        response,
        status,
        completed_us,
        latency_ms,
        billing_usd,
        provenance,
        request_started_us=None,
    ):
        if provenance not in {"original_recording", "mock", "counterfactual_inference"}:
            raise ValueError("model_provenance_required")
        if status not in {"success", "timeout", "error", "abstention", "budget_exhausted"}:
            raise ValueError("model_status_required")
        asof = int(datetime.fromisoformat(request.state["as_of"]).timestamp() * 1_000_000)
        if completed_us < asof or request_started_us is not None and request_started_us < asof:
            raise ValueError("model_completion_precedes_request")
        if latency_ms is not None and (not D(str(latency_ms)).is_finite() or latency_ms < 0):
            raise ValueError("invalid_latency")
        if billing_usd is not None and (not D(billing_usd).is_finite() or D(billing_usd) < 0):
            raise ValueError("invalid_billing")
        record = {
            "response": response,
            "status": status,
            "completed_us": completed_us,
            "request_started_us": request_started_us,
            "latency_ms": latency_ms,
            "billing_usd": billing_usd,
            "provenance": provenance,
            "policy_hash": request.policy_hash,
            "feature_version": request.feature_version,
            "model": request.model,
            "request_hash": request.key,
        }
        require_sanitized(record)
        encoded = canonical(record)
        if len(encoded.encode()) > 1_000_000:
            raise ValueError("model_response_size_budget")
        old = self.db.execute(
            "SELECT hash FROM research_model_cache_v1 WHERE key=?", (request.key,)
        ).fetchone()
        if old:
            if old[0] != digest(record):
                raise ValueError("conflicting_exact_model_response")
            return False
        with self.db:
            self.db.execute(
                "INSERT INTO research_model_cache_v1 VALUES(?,?,?,?)",
                (request.key, request.state_json, encoded, digest(record)),
            )
        return True

    def get(self, request: ModelRequest, now_us: int):
        row = self.db.execute(
            "SELECT record,hash FROM research_model_cache_v1 WHERE key=?", (request.key,)
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row[0])
        if digest(record) != row[1]:
            raise ValueError("model_cache_integrity_failure")
        return record if record["completed_us"] <= now_us else None

    def close(self):
        self.db.close()


class CommandCollector:
    """A closed simulation capability surface, never a broker factory."""

    def __init__(self):
        self.commands = []

    def close_owned(self, owner, **kwargs):
        self.commands.append(serial({"action": "close", "owner": owner, **kwargs}))

    def replace_stop_bounded(self, owner, **kwargs):
        self.commands.append(serial({"action": "stop", "owner": owner, **kwargs}))

    def reduce_owned(self, owner, **kwargs):
        raise ValueError("v5_partial_reduction_forbidden")


class CachedJev:
    def __init__(self, cache: ModelCache):
        self.cache = cache
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.executescript("""
            CREATE TABLE jev_exit_reviews (
                id TEXT PRIMARY KEY, owner TEXT, started_at TEXT, state TEXT, snapshot TEXT,
                response TEXT, outcome TEXT, request_id TEXT);
        """)
        self.worker = object.__new__(JevExitManager)
        self.worker.policy, self.worker.stop_focused = policy, True
        self.worker.db = self.db

    def decide(
        self, request: ModelRequest, now_us: int, *, position=None, quote=None, context=None
    ):
        state = request.state
        result = {
            "request_hash": request.key,
            "policy_hash": request.policy_hash,
            "commands": [],
            "status": "fallback",
            "reason": "exact_cache_missing",
            "quantity": "preserve",
            "stop": "preserve",
            "billing_usd": None,
            "provenance": None,
        }
        if request.policy_hash != policy.POLICY_HASH:
            return {**result, "reason": "policy_hash_mismatch"}
        record = self.cache.get(request, now_us)
        if record is None:
            return result
        result.update(
            billing_usd=record["billing_usd"],
            provenance=record["provenance"],
            latency_ms=record["latency_ms"],
        )
        if record["status"] != "success":
            return {**result, "reason": record["status"]}
        if record["latency_ms"] is None or record["latency_ms"] > 1500:
            return {**result, "reason": "missing_or_excess_latency"}
        asof = int(datetime.fromisoformat(state["as_of"]).timestamp() * 1_000_000)
        if not 0 <= now_us - asof <= 60_000_000:
            return {**result, "reason": "review_expired"}
        response = record["response"]
        if not isinstance(response, dict) or response.get("model") != policy.MODEL:
            return {**result, "reason": "model_mismatch"}
        # The exact current simulated position is mandatory for path-dependent replay.
        if position is None or quote is None:
            return {**result, "reason": "current_position_and_quote_required"}
        p = SimpleNamespace(
            broker_symbol=position["symbol"],
            side=position["side"],
            entry_price=D(position["entry"]),
            quantity=D(position["quantity"]),
            stop_price=D(position["stop"]),
            leverage=D(position["leverage"]),
        )
        owner = state["strategy"] + ":" + state["original"]["opened_at"] + ":" + p.broker_symbol
        review = request.key
        if self.db.execute("SELECT 1 FROM jev_exit_reviews WHERE id=?", (review,)).fetchone():
            return {**result, "reason": "review_already_consumed"}
        self.db.execute(
            "INSERT INTO jev_exit_reviews VALUES(?,?,?,?,?,?,?,?)",
            (
                review,
                owner,
                state["as_of"],
                "pending",
                request.state_json,
                canonical(response),
                None,
                None,
            ),
        )
        collector = CommandCollector()
        self.worker.lifecycle = collector
        self.worker.clock = lambda: utc(now_us)
        # Freshness cannot be inferred from a supplied price alone.
        try:
            observed = datetime.fromisoformat(quote["observed_at"])
            if not 0 <= (utc(now_us) - observed).total_seconds() <= 5:
                raise ValueError("stale_quote")
            self.worker.market = SimpleNamespace(quote=lambda _: deepcopy(quote))
            outcome = self.worker._apply(
                owner,
                review,
                {"snapshot": state, "response": deepcopy(response)},
                p,
                context if context is not None else {},
            )
        except (ValueError, KeyError, TypeError, ArithmeticError):
            return {**result, "reason": "invalid_or_unavailable_current_evidence"}
        row = self.db.execute(
            "SELECT state,outcome FROM jev_exit_reviews WHERE id=?", (review,)
        ).fetchone()
        return {
            **result,
            "status": row[0],
            "reason": row[1],
            "production_outcome": outcome,
            "commands": collector.commands,
        }

    def close(self):
        self.db.close()
