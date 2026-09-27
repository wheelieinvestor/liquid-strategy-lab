"""Authored model-control cases: production gate behavior, not model skill/P&L."""

from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.fixtures import US, jev_inputs, jev_response
from liquid_autonomous_trader.backtesting.jev import CachedJev, ModelCache, build_request


def execute(case):
    if case.family != "jev":
        raise ValueError("jev_case_required")
    base, market = jev_inputs()
    side = 1 if case.direction == "long" else -1
    base["position"]["side"] = case.direction
    base["position"]["quantity"] = str(D(1) + D(case.variant) / 4)
    base["position"]["stop"] = str(D(100) - side * D(10))
    base["original"]["original_stop"] = base["position"]["stop"]
    base["plan"]["target"] = str(D(100) + side * D(25))
    base["mark_price"] = str(D(100) + side * D(10))
    if side < 0:
        market.update(
            bid="89.99",
            ask="90",
            support_prior20="88",
            resistance_prior20="96",
            return_1bar="-.01",
            return_4bar="-.02",
        )
    name = case.archetype
    if name == "bounded_loosen":
        base["position"]["stop"] = str(D(100) + side * D(5))
    if name == "intact_consolidation":
        base["position"]["stop"] = "100"
        market.update(return_1bar="0", return_4bar="0")
    if name == "crossed_stop":
        base["position"]["stop"] = market["bid"] if side > 0 else market["ask"]
        try:
            build_request(base, market)
        except ValueError as error:
            assert str(error) == "production_request_inputs_unavailable"
            result = {
                "case_id": case.case_id,
                "status": "passed",
                "mode": "synthetic_stress",
                "checks": ["crossed_stop_cannot_be_revived_by_model"],
                "model_status": "input_rejected",
                "provenance": "authored_fixture",
                "commands": [],
            }
            return {**result, "core_hash": digest(result)}
        raise AssertionError("crossed_stop_request_was_accepted")
    request = build_request(base, market)
    approve = name in {"tighten", "bounded_loosen", "long_short_symmetry", "repeated_review"}
    action = "CLOSE_ALL" if name in {"full_exit", "thesis_failure"} else "HOLD"
    response = jev_response(request, action=action, approve=approve)
    if name == "bounded_loosen":
        for key, price in request.state["stop_candidates"].items():
            choice = "approve" if side * (D(price) - D(base["position"]["stop"])) < 0 else "reject"
            response["answers"]["price_" + key] = {
                "choice": choice,
                "probabilities": {
                    "approve": int(choice == "approve"),
                    "reject": int(choice == "reject"),
                },
            }
    elif name == "prohibited_partial":
        response["answers"]["action"] = {
            "choice": "REDUCE_50_PERCENT",
            "probabilities": {"REDUCE_50_PERCENT": 1},
        }
    elif name == "malformed_response":
        response["answers"] = {
            "action": {"choice": "CLOSE_ALL", "probabilities": {"CLOSE_ALL": "bad"}}
        }
    elif name == "excess_risk_loosen":
        response["answers"]["price_unoffered_original_risk_breach"] = {
            "choice": "approve",
            "probabilities": {"approve": 1},
        }
        assert all(
            side * (D(p) - D(base["original"]["original_stop"])) >= 0
            for p in request.state["stop_candidates"].values()
        )
    status = {
        "model_outage": "timeout",
        "budget_exhausted": "budget_exhausted",
        "abstain": "abstention",
    }.get(name, "success")
    cache = ModelCache(Path(":memory:"))
    adapter = CachedJev(cache)
    try:
        cache.put(
            request,
            response=response if status == "success" else None,
            status=status,
            completed_us=US + 1000,
            latency_ms=1,
            billing_usd=None,
            provenance="mock",
        )
        now_us = US + 61_000_000 if name == "stale_response" else US + 1000
        result = adapter.decide(request, now_us, position=request.state["position"], quote=market)
        commands = result["commands"]
        if name in {"full_exit", "thesis_failure"}:
            assert len(commands) == 1 and commands[0]["action"] == "close", (
                "clear_full_exit_unavailable"
            )
        elif approve:
            assert len(commands) == 1 and commands[0]["action"] == "stop", (
                "approved_stop_not_applied"
            )
            change = side * (D(commands[0]["stop"]) - D(base["position"]["stop"]))
            assert change < 0 if name == "bounded_loosen" else change > 0
            assert side * (D(commands[0]["stop"]) - D(base["original"]["original_stop"])) >= 0
        else:
            assert not commands, "uncertain_or_unoffered_answer_changed_exposure"
        checks = [
            "actual_v5_action_gates",
            "quantity_not_partially_reduced",
            "original_stop_bound",
            "mock_provenance_retained",
            "unknown_billing_retained",
        ]
        if name == "repeated_review":
            second = adapter.decide(
                request, now_us + 1000, position=request.state["position"], quote=market
            )
            assert second["reason"] == "review_already_consumed" and not second["commands"]
            checks.append("same_review_not_applied_twice")
        record = cache.get(request, now_us)
        assert record["provenance"] == "mock" and record["billing_usd"] is None
        material = {
            "case_id": case.case_id,
            "seed": case.seed,
            "status": "passed",
            "mode": "synthetic_stress",
            "checks": checks,
            "result": result,
            "request_hash": request.key,
            "interpretation": "Authored response-control test, not semantic accuracy "
            "or investment performance",
        }
        return {**material, "core_hash": digest(material)}
    finally:
        adapter.close()
        cache.close()
