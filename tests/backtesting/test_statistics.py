import pytest

from liquid_autonomous_trader.backtesting.statistics import (
    WEEK_US,
    holm,
    paired_uncertainty,
    verdict,
    weekly_pairs,
)


def test_holm_includes_failed_trials_and_is_monotone():
    result = holm({"a": 0.01, "b": 0.03, "failed": None}, total_trials=5)
    assert result == {"a": 0.05, "b": 0.12, "failed": 1}
    with pytest.raises(ValueError, match="undeclared"):
        holm({"a": 0.1, "b": 0.1}, total_trials=1)


def test_paired_economic_blocks_preserve_tail_and_do_not_fill_missing_dates():
    baseline = {
        "start_us": 0,
        "end_us": 2 * WEEK_US + 100,
        "ledger": {"initial_cash": "100"},
        "equity_curve": [
            {"at_us": WEEK_US - 1, "equity": "95"},
            {"at_us": 2 * WEEK_US - 1, "equity": "90"},
            {"at_us": 2 * WEEK_US + 99, "equity": "89"},
        ],
    }
    candidate = {
        **baseline,
        "equity_curve": [
            {"at_us": WEEK_US - 1, "equity": "97"},
            {"at_us": 2 * WEEK_US - 1, "equity": "95"},
            {"at_us": 2 * WEEK_US + 99, "equity": "96"},
        ],
    }
    paired = weekly_pairs(baseline, candidate)
    assert [r["net_delta"] for r in paired["blocks"]] == ["2", "3"]
    assert paired["partial_tail_delta"] == "2"
    candidate["equity_curve"] = candidate["equity_curve"][:-1]
    assert weekly_pairs(baseline, candidate)["status"] == "unsupported"


def test_bootstrap_reproducible_missing_coverage_can_never_improve():
    values = [str(i % 7 - 2) for i in range(30)]
    first = paired_uncertainty(values, seed=123, samples=500)
    assert paired_uncertainty(values, seed=123, samples=500) == first
    assert (
        first["simultaneous_ci95"][0]
        <= first["ci95"][0]
        <= first["ci95"][1]
        <= first["simultaneous_ci95"][1]
    )
    assert (
        verdict(
            first,
            adjusted_p=0,
            coverage_complete=False,
            invariants_passed=True,
            neighboring_signs_stable=True,
            holds_within_dependence_span=True,
        )[0]
        == "unsupported"
    )
    assert paired_uncertainty([], seed=1)["status"] == "insufficient_time_blocks"
    assert (
        paired_uncertainty(["1"] * 30, seed=1)["status"] == "zero_variance_no_distribution_estimate"
    )
