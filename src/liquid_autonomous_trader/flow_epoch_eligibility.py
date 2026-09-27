"""Conservative epoch eligibility from durable execution and dispatch evidence."""

import json
from datetime import datetime
from decimal import Decimal

from liquid_autonomous_trader.frozen.flow_mirror import flow_mirror_risk_epoch


def epoch_blocked_symbols(db, journal, *, now):
    epoch = flow_mirror_risk_epoch(now)
    blocked = set()
    for record in db.execute(
        "SELECT e.*,MIN(l.at) AS reserved_at FROM executions e JOIN lifecycle l "
        "ON l.intent_id=e.intent_id WHERE e.strategy='flow_show_mirror' "
        "AND l.state='reserved' GROUP BY e.intent_id"
    ):
        if flow_mirror_risk_epoch(datetime.fromisoformat(record["reserved_at"])) != epoch:
            continue
        history = db.execute(
            "SELECT state,detail FROM lifecycle WHERE intent_id=? ORDER BY seq",
            (record["intent_id"],),
        ).fetchall()
        # Only the lifecycle's proven pre-dispatch rejection can release epoch
        # eligibility. Filled/closed, ambiguous and broker-rejected attempts retain
        # the existing conservative behavior. Never modify decisions or cursors.
        never_dispatched = (
            record["state"] == "rejected"
            and record["order_id"] is None
            and Decimal(record["filled_quantity"]) == 0
            and Decimal(record["protected_quantity"]) == 0
            and [row["state"] for row in history] == ["reserved", "submitting", "rejected"]
            and json.loads(history[-1]["detail"]).get("reason") == "entry_rejected_before_dispatch"
            and journal.operation(record["intent_id"]) is None
        )
        if not never_dispatched:
            blocked.add(record["symbol"].removeprefix("xyz:"))
    return blocked
