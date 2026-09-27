"""Paired chronological uncertainty; descriptive statistics never grant promotion.

The frozen protocol uses net economic deltas, not Sharpe selection. Holm controls
the five tested policies; circular moving-block bootstrap retains adjacent weekly
dependence. Simultaneous percentile bounds use conservative Bonferroni coverage.
Neither correction repairs missing data, overlapping holds beyond the block span,
nonstationarity or selection on an already exposed final test.
"""

from __future__ import annotations

import math
import random
from decimal import Decimal as D

WEEK_US = 7 * 24 * 3600 * 1_000_000


def weekly_pairs(baseline, candidate):
    if (baseline["start_us"], baseline["end_us"]) != (candidate["start_us"], candidate["end_us"]):
        raise ValueError("paired_run_window_mismatch")
    left, right = baseline["equity_curve"], candidate["equity_curve"]
    if [r["at_us"] for r in left] != [r["at_us"] for r in right]:
        return {"status": "unsupported", "reason": "paired_equity_coverage_mismatch", "blocks": []}
    start = baseline["start_us"]
    previous = D(candidate["ledger"]["initial_cash"]) - D(baseline["ledger"]["initial_cash"])
    points = {r["at_us"]: D(c["equity"]) - D(r["equity"]) for r, c in zip(left, right, strict=True)}
    blocks = []
    boundary = start + WEEK_US
    while boundary <= baseline["end_us"]:
        if boundary - 1 not in points:
            return {"status": "unsupported", "reason": "missing_exact_week_endpoint", "blocks": []}
        value = points[boundary - 1]
        blocks.append(
            {"start_us": boundary - WEEK_US, "end_us": boundary, "net_delta": str(value - previous)}
        )
        previous = value
        boundary += WEEK_US
    final = D(right[-1]["equity"]) - D(left[-1]["equity"]) if left else previous
    return {
        "status": "paired",
        "blocks": blocks,
        "partial_tail_delta": str(final - previous),
        "partial_tail_included_in_net_not_uncertainty": True,
    }


def quantile(values, p):
    if not values or not 0 <= p <= 1:
        raise ValueError("invalid_quantile")
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def paired_uncertainty(deltas, *, seed, trials=5, samples=4000, moving_blocks=2):
    if not 1 <= trials <= 1000 or not 100 <= samples <= 20000 or not 1 <= moving_blocks <= 52:
        raise ValueError("statistical_compute_budget")
    data = [float(D(x)) for x in deltas]
    if not all(math.isfinite(x) for x in data):
        raise ValueError("nonfinite_paired_delta")
    n = len(data)
    mean = sum(data) / n if n else None
    result = {
        "time_blocks": n,
        "moving_block_length": moving_blocks,
        "effective_nonoverlapping_spans": n // moving_blocks,
        "mean_weekly_delta": mean,
        "ci95": None,
        "simultaneous_ci95": None,
        "p_one_sided": None,
        "bootstrap_samples": samples,
        "seed": seed,
        "family_trials": trials,
        "method": "circular moving-block bootstrap of paired weekly net deltas",
        "limitations": [
            "conditional on stationarity and dependence span; not distribution-free",
            "adjacent weekly marks can share an open trade; "
            "holds beyond span invalidate sufficiency",
        ],
    }
    if n < max(2, moving_blocks):
        return {**result, "status": "insufficient_time_blocks"}
    if min(data) == max(data):
        return {**result, "status": "zero_variance_no_distribution_estimate"}
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        sampled = []
        while len(sampled) < n:
            first = rng.randrange(n)
            sampled.extend(data[(first + j) % n] for j in range(moving_blocks))
        means.append(sum(sampled[:n]) / n)
    ci = [quantile(means, 0.025), quantile(means, 0.975)]
    adjusted = [quantile(means, 0.025 / trials), quantile(means, 1 - 0.025 / trials)]
    # Recenter under H0 mean=0. Add-one correction avoids a zero Monte Carlo p.
    p = (1 + sum(sample - mean >= mean for sample in means)) / (samples + 1)
    return {
        **result,
        "status": "descriptive_interval",
        "ci95": ci,
        "simultaneous_ci95": adjusted,
        "p_one_sided": p,
        "precision_halfwidth": (ci[1] - ci[0]) / 2,
    }


def holm(p_values, *, total_trials=5):
    if len(p_values) > total_trials:
        raise ValueError("undeclared_extra_hypothesis")
    # Failed, unavailable and baseline trials remain in the multiplicity count.
    supplied = {key: 1.0 if value is None else float(value) for key, value in p_values.items()}
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in supplied.values()):
        raise ValueError("invalid_p_value")
    ordered = sorted(supplied, key=lambda k: (supplied[k], k))
    previous = 0
    result = {}
    for rank, key in enumerate(ordered):
        previous = max(previous, min(1, supplied[key] * (total_trials - rank)))
        result[key] = previous
    return result


def verdict(
    interval,
    *,
    adjusted_p,
    coverage_complete,
    invariants_passed,
    neighboring_signs_stable,
    holds_within_dependence_span,
):
    if not invariants_passed:
        return "rejected", ["safety_or_accounting_invariant_failure"]
    if not coverage_complete:
        return "unsupported", ["missing_native_or_exact_model_coverage"]
    reasons = []
    if interval["effective_nonoverlapping_spans"] < 12:
        reasons.append("fewer_than_12_dependence_spans")
    if not holds_within_dependence_span:
        reasons.append("holding_overlap_exceeds_dependence_span")
    if interval["status"] != "descriptive_interval":
        reasons.append(interval["status"])
    if reasons:
        return "inconclusive", reasons
    if interval["ci95"][1] < 0:
        return "rejected", ["negative_paired_net_delta"]
    if adjusted_p >= 0.05 or interval["simultaneous_ci95"][0] <= 0:
        reasons.append("adjusted_improvement_not_established")
    if interval["precision_halfwidth"] > abs(interval["mean_weekly_delta"]) / 2:
        reasons.append("precision_insufficient")
    if not neighboring_signs_stable:
        reasons.append("neighboring_parameters_unstable")
    return ("inconclusive", reasons) if reasons else ("improves", [])
