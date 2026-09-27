from datetime import timedelta

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.fixtures import NOW, US, jev_inputs, jev_response
from liquid_autonomous_trader.backtesting.jev import ModelCache, build_request
from liquid_autonomous_trader.backtesting.recorded_models import import_recorded_models


def evidence():
    base, market = jev_inputs()
    request = build_request(base, market)
    response = jev_response(request)
    row = {
        "research_review_id": "a" * 64,
        "research_owner_id": "b" * 64,
        "started_at": NOW.isoformat(),
        "state": "applied",
        "outcome": "HOLD",
        "snapshot": request.state,
        "response": response,
        "original_request_state_sha256": digest(request.state),
        "response_sha256": digest(response),
        "policy_hash": request.policy_hash,
        "latency_ms": 50,
        "cost_usd": ".01",
    }
    return request, {
        "schema": "liquid-sanitized-evidence-v1",
        "production_writes": False,
        "model_inference": False,
        "observed_at": (NOW + timedelta(days=1)).isoformat(),
        "model_records": [row],
    }


def test_recorded_response_is_unavailable_until_observed_receipt(tmp_path):
    request, data = evidence()
    cache = ModelCache(tmp_path / "models.sqlite")
    first = import_recorded_models(data, cache)
    assert first["counts"] == {"original_recording_cached": 1}
    assert cache.get(request, US + 500_000) is None
    saved = cache.get(request, US + 86400_000_000)
    assert saved["billing_usd"] is None
    assert saved["response"] == data["model_records"][0]["response"]
    second = import_recorded_models(data, cache)
    assert first["core_sha256"] == second["core_sha256"]
    assert second["inserted_this_invocation"] == 0
    cache.close()


def test_ambiguous_quote_cutoff_is_retained_as_unsupported(tmp_path):
    request, data = evidence()
    row = data["model_records"][0]
    row["snapshot"]["market"]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    row["original_request_state_sha256"] = digest(row["snapshot"])
    cache = ModelCache(tmp_path / "models.sqlite")
    result = import_recorded_models(data, cache)
    assert result["counts"] == {"future_model_quote": 1}
    assert cache.db.execute("SELECT COUNT(*) FROM research_model_cache_v1").fetchone()[0] == 0
    cache.close()
