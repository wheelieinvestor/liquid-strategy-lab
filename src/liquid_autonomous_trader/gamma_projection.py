"""Approved QQQ strike projection; persistent expiry and closed-bar geometry.

Consumes archived public market evidence, never broker state or write authority.
"""

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from liquid_autonomous_trader.cash_calendar import xnys_session
from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.frozen.itmatrix_xyz100 import ITMatrixGexRecordV1

SPEC = {
    "id": "qqq-nearest-expiry-absolute-gex-v1",
    "approved": "2026-09-09",
    "expiration": "nearest_unexpired_frozen_per_cash_session",
    "metric": "totalGexDollars",
    "distance_fraction": "0.02",
    "per_side": 2,
    "tie_break": ["absolute_gex_desc", "spot_distance_asc", "strike_asc"],
    "missing_pair": "hold",
    "missing_zero_gamma": None,
}
SPEC_SHA256 = digest(SPEC)


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("gamma_number_invalid")
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError("gamma_number_invalid") from None
    if not result.is_finite() or abs(result) >= Decimal("1e28") or (positive and result <= 0):
        raise ValueError("gamma_number_invalid")
    return result


class GammaProjection:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS gamma_expiry_selection(
                session TEXT PRIMARY KEY, expiry TEXT NOT NULL, spec_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS gamma_source_revisions(
                expiry TEXT NOT NULL, observed_at TEXT NOT NULL, content_hash TEXT NOT NULL,
                PRIMARY KEY(expiry, observed_at));
            CREATE TABLE IF NOT EXISTS gamma_bar_geometry(
                session TEXT NOT NULL, bar_end TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(session,bar_end));
        """)

    def project(self, matrix, *, raw_sha256, fetched_at, now, bar_end, freeze=True):
        """One frozen four-level map per settled bar; missing inputs cause HOLD upstream."""
        if any(t.tzinfo is None for t in (fetched_at, now, bar_end)):
            raise ValueError("gamma_aware_times_required")
        day = now.astimezone(ZoneInfo("America/New_York")).date()
        session = xnys_session(day)
        if session is None or not session.open_at <= now < session.close_at:
            raise ValueError("gamma_cash_session_closed")
        if (
            fetched_at > now
            or (now - fetched_at).total_seconds() > 300
            or bar_end.timestamp() != int((now.timestamp() - 2) // 900) * 900
            or not session.open_at < bar_end <= session.close_at
        ):
            raise ValueError("gamma_fresh_settled_bar_required")
        import re

        if not isinstance(raw_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", raw_sha256):
            raise ValueError("gamma_raw_evidence_required")
        if not isinstance(matrix, dict) or matrix.get("symbol") != "QQQ":
            raise ValueError("gamma_qqq_matrix_required")
        expirations = matrix.get("expirations")
        if not isinstance(expirations, list) or not 1 <= len(expirations) <= 64:
            raise ValueError("gamma_expiration_inventory_invalid")
        indexed = {}
        for row in expirations:
            expiry = date.fromisoformat(row["expiration"])
            if row.get("symbol") != "QQQ" or expiry in indexed:
                raise ValueError("gamma_expiration_identity_invalid")
            indexed[expiry] = row
        candidates = [expiry for expiry in indexed if expiry >= day]
        if not candidates:
            raise ValueError("gamma_unexpired_expiration_missing")
        key = day.isoformat()
        selected = self.db.execute(
            "SELECT expiry,spec_hash FROM gamma_expiry_selection WHERE session=?", (key,)
        ).fetchone()
        if selected:
            expiry = date.fromisoformat(selected[0])
            if selected[1] != SPEC_SHA256 or expiry not in indexed:
                raise ValueError("gamma_frozen_expiration_unavailable")
        else:
            expiry = min(candidates)
            self.db.execute(
                "INSERT INTO gamma_expiry_selection VALUES(?,?,?)",
                (key, expiry.isoformat(), SPEC_SHA256),
            )
        row = indexed[expiry]
        observed = datetime.fromisoformat(row["updatedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not session.open_at <= observed <= fetched_at:
            raise ValueError("gamma_source_time_invalid")
        if not 0 <= (now - observed).total_seconds() <= 300:
            raise ValueError("gamma_source_stale")
        revision_key = (expiry.isoformat(), observed.isoformat())
        revision = self.db.execute(
            "SELECT content_hash FROM gamma_source_revisions WHERE expiry=? AND observed_at=?",
            revision_key,
        ).fetchone()
        content_hash = digest(row)
        if revision and revision[0] != content_hash:
            raise ValueError("gamma_conflicting_source_revision")
        self.db.execute(
            "INSERT OR IGNORE INTO gamma_source_revisions VALUES(?,?,?)",
            (*revision_key, content_hash),
        )
        previous = self.db.execute(
            "SELECT body FROM gamma_bar_geometry WHERE session=? AND bar_end=?",
            (key, bar_end.isoformat()),
        ).fetchone()
        if previous:
            body = json.loads(previous[0])
            record = ITMatrixGexRecordV1.model_validate(body["record"])
            if (now - record.source_timestamp).total_seconds() > 300:
                raise ValueError("gamma_frozen_bar_geometry_stale")
            return record, body["provenance"]
        spot = number(row["spotPrice"], positive=True)
        net = number(row["netGex"]["totalGexDollars"])
        strikes, by_strike = row.get("strikes"), row.get("gexByStrike")
        if (
            not isinstance(strikes, list)
            or not 1 <= len(strikes) <= 5000
            or not isinstance(by_strike, dict)
        ):
            raise ValueError("gamma_strike_inventory_invalid")
        declared = [number(s, positive=True) for s in strikes]
        actual = [
            (number(k, positive=True), number(v["totalGexDollars"])) for k, v in by_strike.items()
        ]
        if (
            len(set(declared)) != len(declared)
            or len({k for k, v in actual}) != len(actual)
            or set(declared) != {k for k, v in actual}
        ):
            raise ValueError("gamma_strike_inventory_mismatch")
        eligible = [(k, v) for k, v in actual if v != 0 and abs(k / spot - 1) <= Decimal("0.02")]

        def side_levels(below):
            values = [(k, v) for k, v in eligible if (k < spot if below else k > spot)]
            values.sort(key=lambda p: (-abs(p[1]), abs(p[0] - spot), p[0]))
            if len(values) < 2:
                raise ValueError("gamma_four_distinct_levels_unavailable")
            return sorted(k for k, v in values[:2])

        down, support = side_levels(True)
        resistance, up = side_levels(False)
        zero = row.get("zeroGammaLevel")
        zero = None if zero is None else number(zero)
        zero = zero if zero is not None and zero > 0 else None
        record = ITMatrixGexRecordV1(
            symbol="QQQ",
            source_timestamp=observed,
            complete=True,
            net_gex_dollars=net,
            zero_gamma_level=zero,
            downside_target=down,
            support_level=support,
            resistance_level=resistance,
            upside_target=up,
            raw_response_sha256=raw_sha256,
        )
        provenance = {
            "spec_id": SPEC["id"],
            "spec_sha256": SPEC_SHA256,
            "expiration": expiry.isoformat(),
            "bar_end": bar_end.isoformat(),
            "source_content_sha256": content_hash,
            "spot": str(spot),
        }
        if freeze:
            self.freeze(record, provenance)
        return record, provenance

    def freeze(self, record, provenance):
        key = record.source_timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        body = {"record": record.model_dump(mode="json"), "provenance": provenance}
        self.db.execute(
            "INSERT INTO gamma_bar_geometry VALUES(?,?,?)",
            (key, provenance["bar_end"], json.dumps(body, sort_keys=True)),
        )
