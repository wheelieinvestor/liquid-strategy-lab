import copy
from datetime import UTC, datetime

import pytest

from liquid_autonomous_trader import jev_stop_policy as policy
from liquid_autonomous_trader.backtesting.events import canonical
from liquid_autonomous_trader.backtesting.jev import (
    CachedJev,
    ModelCache,
    ModelRequest,
    build_request,
)

NOW = datetime(2026, 9, 22, 15, 1, tzinfo=UTC)
US = int(NOW.timestamp() * 1_000_000)


def inputs():
    position = {
        "symbol": "BTC",
        "side": "long",
        "entry": "100",
        "quantity": "1",
        "stop": "90",
        "leverage": "40",
    }
    base = {
        "schema_version": policy.VERSION,
        "model": policy.MODEL,
        "strategy": "btc_momentum",
        "as_of": NOW.isoformat(),
        "position": position,
        "original": {
            "entry": "100",
            "original_stop": "90",
            "risk_price": "10",
            "price_risk_budget": "50",
            "opened_at": "2026-09-22T14:00:00+00:00",
            "entry_plan": {},
        },
        "plan": {"target": "125"},
        "context": {},
        "review_r": "1",
        "mark_price": "110",
        "peak_r": "1",
        "age_seconds": 3660,
        "previous": None,
        "confirmed_reductions": [],
    }
    market = {
        "symbol": "BTC",
        "bid": "110",
        "ask": "110.01",
        "observed_at": NOW.isoformat(),
        "price_step": ".01",
        "quantity_step": ".01",
        "maximum_leverage": "40",
        "atr14_price": "2",
        "support_prior20": "104",
        "resistance_prior20": "112",
        "return_1bar": ".01",
        "return_4bar": ".02",
        "bar_end_ms": int(NOW.replace(minute=0).timestamp() * 1000) - 1,
    }
    return base, market


def response(request, action="HOLD", approve=True):
    def answer(choice, options):
        return {"choice": choice, "probabilities": {k: int(k == choice) for k in options}}

    return {
        "model": policy.MODEL,
        "answers": {
            "action": answer(action, policy.ACTION),
            **{
                "price_" + k: answer("approve" if approve else "reject", policy.PRICE_CRITERIA)
                for k in request.state["stop_candidates"]
            },
        },
    }


def cached(tmp_path, action="HOLD", **changes):
    base, market = inputs()
    request = build_request(base, market)
    cache = ModelCache(tmp_path / "cache.sqlite")
    record = dict(
        response=response(request, action),
        status="success",
        completed_us=US + 1000,
        latency_ms=1,
        billing_usd=None,
        provenance="mock",
    )
    record.update(changes)
    cache.put(request, **record)
    return request, CachedJev(cache), market


def test_request_uses_production_candidates_and_exact_cache_binding(tmp_path):
    req, adapter, market = cached(tmp_path)
    state = req.state
    assert state["quantity_policy"]["partials"] == "disabled"
    assert state["reduction_quantities"] == {}
    result = adapter.decide(req, US + 1000, position=state["position"], quote=market)
    assert result["production_outcome"] == "jev_change_stop"
    expected, _ = policy.select_stop(state, response(req)["answers"])
    assert result["commands"][0]["stop"] == state["stop_candidates"][expected]
    assert result["provenance"] == "mock" and result["billing_usd"] is None
    changed = copy.deepcopy(state)
    changed["position"]["quantity"] = "0.5"
    assert (
        adapter.decide(ModelRequest(canonical(changed)), US + 1000)["reason"]
        == "exact_cache_missing"
    )
    assert (
        adapter.decide(req, US + 2000, position=state["position"], quote=market)["reason"]
        == "review_already_consumed"
    )


@pytest.mark.parametrize("status", ["timeout", "error", "abstention", "budget_exhausted"])
def test_failed_model_responses_are_retained_and_preserve_quantity(tmp_path, status):
    req, adapter, market = cached(tmp_path, status=status, response=None)
    result = adapter.decide(req, US + 1000, position=req.state["position"], quote=market)
    assert result["reason"] == status and not result["commands"]
    assert adapter.cache.get(req, US) is None  # future model completion unavailable
    assert adapter.cache.get(req, US + 1000)["status"] == status


def test_clear_full_exit_and_malicious_partial_are_gated_by_actual_policy(tmp_path):
    req, adapter, market = cached(tmp_path, action="CLOSE_ALL")
    result = adapter.decide(req, US + 1000, position=req.state["position"], quote=market)
    assert [c["action"] for c in result["commands"]] == ["close"]
    base, market = inputs()
    req = build_request(base, market)
    cache = ModelCache(tmp_path / "malformed.sqlite")
    bad = response(req, approve=False)
    bad["answers"]["action"] = {
        "choice": "REDUCE_50_PERCENT",
        "probabilities": {"REDUCE_50_PERCENT": 1},
    }
    cache.put(
        req,
        response=bad,
        status="success",
        completed_us=US + 1000,
        latency_ms=1,
        billing_usd="0",
        provenance="mock",
    )
    assert (
        CachedJev(cache).decide(req, US + 1000, position=req.state["position"], quote=market)[
            "commands"
        ]
        == []
    )


def test_labels_and_future_bars_never_enter_model_request():
    base, market = inputs()
    base["expected_action"] = "HOLD"
    with pytest.raises(ValueError, match="future_or_labels"):
        build_request(base, market)
    base, market = inputs()
    market["bar_end_ms"] += 900000
    with pytest.raises(ValueError, match="future_model_bar"):
        build_request(base, market)


def test_changed_current_position_does_not_execute_old_model_answer(tmp_path):
    req, adapter, market = cached(tmp_path)
    p = {**req.state["position"], "stop": "95"}
    result = adapter.decide(req, US + 1000, position=p, quote=market)
    assert result["reason"] == "position_or_context_changed" and result["commands"] == []
