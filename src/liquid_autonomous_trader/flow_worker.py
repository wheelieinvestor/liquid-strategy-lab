"""Pure captured-Flow validation; no polling worker is distributed."""

import json

from liquid_autonomous_trader.desk_store import DeskStore, digest
from liquid_autonomous_trader.flow_inputs import CapturedFlowDeliveryV1


def _require_captured(store: DeskStore, captured: CapturedFlowDeliveryV1) -> str:
    source_event_id = "flow-delivery:" + captured.signal.delivery_event_id
    row = store.db.execute(
        "SELECT input_hash, body, event_hash FROM events WHERE event_id=?", (source_event_id,)
    ).fetchone()
    if row is None or row["input_hash"] != digest(captured.signal.model_dump(mode="json")):
        raise ValueError("canonical_flow_capture_required")
    body = json.loads(row["body"])
    if (
        body.get("status") != "SOURCE_CAPTURED_NOT_EVALUATED"
        or body.get("source") != captured.source
        or body.get("source_contract") != captured.source_contract
        or body.get("signal") != captured.signal.model_dump(mode="json")
    ):
        raise ValueError("canonical_flow_capture_required")
    return str(row["event_hash"])


def _captured_from_row(row) -> CapturedFlowDeliveryV1:
    body = json.loads(row["body"])
    if body.get("status") != "SOURCE_CAPTURED_NOT_EVALUATED":
        raise ValueError("canonical_flow_capture_required")
    return CapturedFlowDeliveryV1(
        source=body.get("source"),
        source_contract=body.get("source_contract"),
        signal=body.get("signal"),
        captured_at=body.get("observed_at"),
        capture_receipt_sha256=digest(body),
    )
