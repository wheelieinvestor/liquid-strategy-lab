"""Immutable, content-addressed research evidence and causal replay.

SQLite is the initial canonical store: transactional dedupe/checkpoints, Decimal
strings and no new dependency. Bulk columnar export can be added independently.
This module has no network, production database or execution dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

QUALITIES = {"observed", "partial", "unsupported", "synthetic_assumption"}
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "private_key",
    "secret",
    "access_token",
    "refresh_token",
    "authorization",
    "wallet_address",
    "account_address",
    "account_id",
    "broker_account_id",
    "clearinghouse_state",
    "clearinghousestate",
    "user_state",
    "userstate",
    "password",
    "secret_key",
    "dsn",
}


def require_sanitized(value):
    """Reject credential/account material before storing generic imported evidence.

    Sanitization belongs in a bounded source-specific exporter; silently deleting
    arbitrary evidence here could hide an identity mismatch. Rejection text never
    includes the offending value.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key).casefold().replace("-", "_")
            if normalized in SENSITIVE_KEYS or key.casefold() in SENSITIVE_KEYS:
                raise ValueError("sensitive_payload_requires_sanitized_export")
            require_sanitized(item)
    elif isinstance(value, list):
        for item in value:
            require_sanitized(item)
    elif isinstance(value, str):
        if re.search(r"(?i)\b0x[0-9a-f]{40}\b|-----BEGIN .*PRIVATE KEY-----", value):
            raise ValueError("raw_account_or_secret_material_forbidden")
        if value.startswith(("http://", "https://", "postgres://", "postgresql://")):
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("payload_url_requires_sanitization")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Event:
    """Times are UTC epoch microseconds; payload numbers use decimal strings.

    Publication can precede event completion (e.g. an advertised settlement).
    Availability cannot precede publication or event completion. Revisions are
    separate events, never updates to previously exposed observations.
    """

    source: str
    instrument: str
    venue: str
    kind: str
    event_us: int
    published_us: int
    available_us: int
    retrieved_us: int
    sequence: int
    revision: int
    units: str
    quality: str
    reference: str
    payload_json: str
    lineage: tuple[str, ...] = ()
    schema: str = "liquid-research-event-v1"

    def __post_init__(self) -> None:
        if self.quality not in QUALITIES:
            raise ValueError("unknown_quality")
        if min(self.event_us, self.published_us, self.sequence, self.revision) < 0:
            raise ValueError("negative_time_or_sequence")
        if self.available_us < max(self.event_us, self.published_us):
            raise ValueError("availability_precedes_evidence")
        if self.retrieved_us < self.available_us:
            raise ValueError("retrieval_precedes_availability")
        if not all((self.source, self.instrument, self.venue, self.kind, self.units)):
            raise ValueError("missing_identity_or_units")
        if canonical(json.loads(self.payload_json)) != self.payload_json:
            raise ValueError("noncanonical_payload")
        require_sanitized(json.loads(self.payload_json))
        require_sanitized([self.source, self.instrument, self.venue, self.reference])
        if "?" in self.reference or "@" in self.reference:
            raise ValueError("reference_must_be_sanitized")

    @property
    def identity(self) -> str:
        return digest(
            [
                self.source,
                self.venue,
                self.instrument,
                self.kind,
                self.event_us,
                self.sequence,
                self.revision,
            ]
        )

    @property
    def content_hash(self) -> str:
        # Retrieval time is acquisition metadata, not a revised market observation.
        value = asdict(self)
        value.pop("retrieved_us")
        return digest(value)

    @property
    def payload(self) -> Any:
        return json.loads(self.payload_json)

    @property
    def order(self) -> tuple:
        return self.available_us, self.event_us, self.sequence, self.identity


class Catalog:
    """Dedicated research DB with append-only evidence and atomic cursor commits."""

    def __init__(self, path: Path, *, max_bytes: int = 512 * 1024 * 1024, read_only: bool = False):
        self.path = path
        self.max_bytes = max_bytes
        if path.exists():
            probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {
                    r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if tables and "research_catalog_identity" not in tables:
                    raise ValueError("not_a_research_catalog")
            finally:
                probe.close()
        self.db = (
            sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            if read_only
            else sqlite3.connect(path)
        )
        if read_only:
            return
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS research_catalog_identity (version INTEGER NOT NULL);
            INSERT INTO research_catalog_identity SELECT 1
              WHERE NOT EXISTS (SELECT 1 FROM research_catalog_identity);
            CREATE TABLE IF NOT EXISTS events (
                identity TEXT PRIMARY KEY, hash TEXT NOT NULL, available_us INTEGER NOT NULL,
                event_us INTEGER NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
              BEGIN SELECT RAISE(ABORT, 'immutable_event'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
              BEGIN SELECT RAISE(ABORT, 'immutable_event'); END;
            CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exposures (
                id INTEGER PRIMARY KEY, dataset_hash TEXT NOT NULL,
                purpose TEXT NOT NULL, first_us INTEGER NOT NULL, last_us INTEGER NOT NULL);
        """)
        self.db.commit()

    def append(self, events: list[Event], *, cursor: tuple[str, str] | None = None) -> int:
        serialized = [(e, canonical(asdict(e))) for e in events]
        estimate = sum(len(body.encode()) * 2 + 1024 for _, body in serialized)
        if self.path.stat().st_size + estimate > self.max_bytes:
            raise ValueError("research_disk_budget_exceeded")
        inserted = 0
        with self.db:
            for event, body in serialized:
                old = self.db.execute(
                    "SELECT hash FROM events WHERE identity=?", (event.identity,)
                ).fetchone()
                if old:
                    if old[0] != event.content_hash:
                        raise ValueError("conflicting_revision_requires_new_revision")
                    continue
                self.db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?)",
                    (
                        event.identity,
                        event.content_hash,
                        event.available_us,
                        event.event_us,
                        event.sequence,
                        body,
                    ),
                )
                inserted += 1
            if cursor:
                self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", cursor)
        return inserted

    def replay(self, *, until_us: int):
        rows = self.db.execute(
            "SELECT body FROM events WHERE available_us<=? "
            "ORDER BY available_us,event_us,sequence,identity",
            (until_us,),
        )
        for (body,) in rows:
            data = json.loads(body)
            data["lineage"] = tuple(data["lineage"])
            yield Event(**data)

    def latest(
        self, source: str, kind: str, instrument: str, event_us: int, *, sequence: int = 0
    ) -> Event | None:
        rows = self.db.execute("SELECT body FROM events WHERE event_us=?", (event_us,))
        matches = []
        for (body,) in rows:
            value = json.loads(body)
            if (value["source"], value["kind"], value["instrument"], value["sequence"]) == (
                source,
                kind,
                instrument,
                sequence,
            ):
                value["lineage"] = tuple(value["lineage"])
                matches.append(Event(**value))
        return max(matches, key=lambda e: e.revision) if matches else None

    def cursor(self, source: str) -> str | None:
        row = self.db.execute("SELECT value FROM cursors WHERE source=?", (source,)).fetchone()
        return row[0] if row else None

    def expose(self, dataset_hash: str, purpose: str, first_us: int, last_us: int) -> None:
        if last_us < first_us or not purpose:
            raise ValueError("invalid_exposure")
        with self.db:
            self.db.execute(
                "INSERT INTO exposures VALUES (NULL,?,?,?,?)",
                (dataset_hash, purpose, first_us, last_us),
            )

    def manifest(self) -> dict:
        hashes = [row[0] for row in self.db.execute("SELECT hash FROM events ORDER BY identity")]
        return {
            "schema": "liquid-research-catalog-v1",
            "event_count": len(hashes),
            "content_hash": digest(hashes),
        }

    def close(self) -> None:
        self.db.close()
