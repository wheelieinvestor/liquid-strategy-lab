from copy import deepcopy
from datetime import timedelta

import pytest

from liquid_autonomous_trader.backtesting.fixtures import (
    NOW,
    cramer_payload,
    flow_payload,
    gamma_events,
)
from liquid_autonomous_trader.backtesting.source_import import normalize_sources
from liquid_autonomous_trader.flow_inputs import CapturedFlowDeliveryV1


def inputs():
    delivery = flow_payload()
    body = {
        "source": delivery["source"],
        "source_contract": delivery["source_contract"],
        "signal": delivery["signal"],
        "status": "SOURCE_CAPTURED_NOT_EVALUATED",
        "observed_at": NOW.isoformat(),
    }
    records = {
        "schema": "liquid-sanitized-evidence-v1",
        "observed_at": (NOW + timedelta(hours=1)).isoformat(),
        "production_writes": False,
        "model_inference": False,
        "flow_deliveries": [{"seq": 1, "body": body, "event_hash": "a" * 64}],
    }
    archives = {
        "schema": "liquid-source-archive-export-v1",
        "observed_at": records["observed_at"],
        "database_writes": False,
        "source_calls": False,
        "inference_calls": False,
        "cramer": [
            {"seq": 1, **cramer_payload(), "created_at": (NOW + timedelta(seconds=3)).isoformat()}
        ],
        "gamma": [
            {
                "kind": e.kind.removeprefix("gamma_"),
                "result": {**e.payload, "symbol": "QQQ"},
                "fetched_at": (NOW + timedelta(seconds=2)).isoformat(),
                "raw_sha256": "b" * 64,
                "artifact_sha256": "c" * 64,
            }
            for e in gamma_events()
            if e.kind in {"gamma_matrix", "gamma_spot"}
        ],
    }
    return records, archives


def test_source_arrival_and_classification_completion_are_preserved():
    records, archives = inputs()
    events, receipt = normalize_sources(records, archives)
    assert receipt["events"] == 4
    cramer = next(e for e in events if e.kind == "cramer_classification")
    assert cramer.available_us - cramer.published_us == 3_000_000
    assert cramer.quality == "partial"
    flow = next(e for e in events if e.kind == "flow_delivery")
    assert flow.available_us < flow.retrieved_us
    assert (
        flow.payload["signal"]
        == CapturedFlowDeliveryV1(**flow_payload()).model_dump(mode="json")["signal"]
    )


def test_future_classification_or_private_source_is_refused():
    records, archives = inputs()
    archives["cramer"][0]["created_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="classification_precedes"):
        normalize_sources(records, archives)
    records, archives = inputs()
    records = deepcopy(records)
    records["secret"] = "do-not-echo"
    with pytest.raises(ValueError, match="sensitive_payload"):
        normalize_sources(records, archives)
