"""Retain original model observations without inventing their completion times."""

from __future__ import annotations

from collections import Counter

from liquid_autonomous_trader import jev_stop_policy as policy
from liquid_autonomous_trader.backtesting.events import canonical, digest, require_sanitized
from liquid_autonomous_trader.backtesting.jev import ModelRequest
from liquid_autonomous_trader.backtesting.source_import import micros


def import_recorded_models(evidence, cache):
    require_sanitized(evidence)
    if (
        evidence["schema"] != "liquid-sanitized-evidence-v1"
        or evidence["production_writes"]
        or evidence["model_inference"]
    ):
        raise ValueError("readonly_model_export_required")
    receipt_us = micros(evidence["observed_at"])
    rows, counts = [], Counter()
    inserted = 0
    for raw in evidence["model_records"]:
        row = {
            "review_id": raw["research_review_id"],
            "owner_id": raw["research_owner_id"],
            "recorded_at": raw["started_at"],
            "recorded_state": raw["state"],
            "recorded_outcome": raw["outcome"],
            "strategy": raw["snapshot"]["strategy"],
            "original_request_state_sha256": raw["original_request_state_sha256"],
        }
        if (
            digest(raw["snapshot"]) != raw["original_request_state_sha256"]
            or digest(raw["response"]) != raw["response_sha256"]
        ):
            raise ValueError("model_export_integrity_mismatch")
        try:
            request = ModelRequest(canonical(raw["snapshot"]), policy_hash=raw["policy_hash"])
            if request.policy_hash != policy.POLICY_HASH:
                raise ValueError("unsupported_recorded_policy_hash")
        except ValueError as error:
            reason = str(error)
            reason = (
                reason
                if reason.replace("_", "").isalnum() and len(reason) < 100
                else type(error).__name__
            )
            row.update(status="unsupported", reason=reason)
            counts[reason] += 1
            rows.append(row)
            continue
        if raw["state"] == "pending":
            row.update(status="unsupported", reason="incomplete_inflight_review")
            counts[row["reason"]] += 1
            rows.append(row)
            continue
        status = (
            "timeout"
            if raw["outcome"] == "TimeoutError"
            else "success"
            if raw["response"] is not None
            else "error"
        )
        # The source has start + model latency, but no authoritative completion
        # time (input fetch and acceptance can take additional time). This late
        # receipt is a conservative availability upper bound, never start+latency.
        inserted += cache.put(
            request,
            response=raw["response"],
            status=status,
            completed_us=receipt_us,
            request_started_us=micros(raw["started_at"]),
            latency_ms=raw["latency_ms"],
            billing_usd=None,
            provenance="original_recording",
        )
        row.update(
            status="original_recording_cached",
            request_hash=request.key,
            cache_available_us=receipt_us,
            completion_basis="conservative_export_receipt_upper_bound",
            model_status=status,
        )
        counts["original_recording_cached"] += 1
        if status == "success":
            # Gate selection on the ORIGINAL state only. No altered position,
            # future path, fresh inference, or portfolio performance is implied.
            answers = raw["response"].get("answers", {})
            exposure, exposure_reason = policy.select_exposure(request.state, answers)
            stop, stop_reason = policy.select_stop(request.state, answers)
            row["original_state_policy_selection"] = {
                "exposure": exposure,
                "exposure_reason": exposure_reason,
                "stop": str(stop) if stop is not None else None,
                "stop_reason": stop_reason,
            }
        rows.append(row)
    result = {
        "schema": "liquid-recorded-model-audit-v1",
        "evidence_sha256": digest(evidence),
        "counts": dict(counts),
        "reviews": rows,
        "inserted_this_invocation": inserted,
        "sample_rule": "exported chronological current-policy prefix; outcomes not selected",
        "coverage_denominator": evidence.get("model_coverage", []),
        "exact_historical_completion_timestamps": 0,
        "fresh_inference_count": 0,
        "billing_usd": None,
        "verdict": "unsupported_for_counterfactual_exit_performance",
        "limitations": [
            "snapshot as_of precedes some fetched quotes; exact decision-time cutoff "
            "unavailable for those rows",
            "source stores model latency but not complete response arrival; "
            "cache available only at export receipt",
            "source responses include later acceptance annotations; raw answers retained unchanged",
            "cost_usd in production is an estimate or reservation, not verified billing",
            "policy selection is original-state gate behavior, not full acceptance "
            "or a profitable exit",
            "changed entry, quantity, stop, context or policy cannot reuse the response",
        ],
    }
    result["core_sha256"] = digest(
        {k: v for k, v in result.items() if k != "inserted_this_invocation"}
    )
    return result
