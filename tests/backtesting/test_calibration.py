from copy import deepcopy
from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting.calibration import calibrate


def evidence():
    rows = []
    for identity, time, price, fee in [
        ("a", 1000, "100", ".1"),
        ("b", 2000, "200", ".2"),
        ("c", 4000, "1000", "5"),
    ]:
        rows.append(
            {
                "fill_id": identity,
                "order_id": identity,
                "time": time,
                "coin": "BTC",
                "crossed": True,
                "operation_kind": "entry",
                "px": price,
                "sz": "1",
                "fee": fee,
                "builder_fee_included": ".05",
                "feeToken": "USDC",
                "attribution": "exact_order_symbol_route",
            }
        )
    return {
        "identity_matches_runtime_pin": True,
        "operation_route_verified": True,
        "route": "live",
        "observed_at": "2026-09-26T00:00:00+00:00",
        "fills": rows,
    }


def test_fee_builder_component_and_validation_are_not_added_to_training():
    result = calibrate(evidence(), split_ms=3000)
    group = result["groups"][0]
    assert D(group["training_fees_usd"]) == D(".3")
    assert D(group["estimated_fee_rate"]) == D(".001")
    # Held-out fee=5, predicted=1000 * .001 = 1; residual=4.
    assert D(group["validation_residual_mean_usd"]) == 4


def test_partial_order_split_and_unmatched_are_excluded():
    data = evidence()
    data["fills"][2]["order_id"] = "a"
    extra = deepcopy(data["fills"][1])
    extra.update(fill_id="unmatched", attribution="unmatched")
    data["fills"].extend([extra, data["fills"][1]])
    result = calibrate(data, split_ms=3000)
    assert result["excluded"] == {"unmatched_order_symbol_route": 1, "order_crosses_split": 2}
    assert result["groups"][0]["training_fills"] == 1
    assert result["groups"][0]["validation_residual_mean_usd"] is None


def test_binding_and_conflicting_native_fill_fail_closed():
    data = evidence()
    data["identity_matches_runtime_pin"] = False
    with pytest.raises(ValueError, match="account_route_binding_required"):
        calibrate(data, split_ms=3000)
    data = evidence()
    extra = dict(data["fills"][0], fee="10")
    data["fills"].append(extra)
    with pytest.raises(ValueError, match="conflicting_native_fill"):
        calibrate(data, split_ms=3000)
