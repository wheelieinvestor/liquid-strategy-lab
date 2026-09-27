"""Local shadow ledger. No credentials, account API, or order transport."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class LedgerConflict(ValueError):
    pass


class DeskStore:
    def __init__(self, path: Path, *, create: bool = False, read_only: bool = False):
        self.path = path
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        elif not path.is_file():
            raise FileNotFoundError("desk_not_initialized")
        if create and read_only:
            raise ValueError("cannot_create_readonly")
        self.db = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro" if read_only else path,
            uri=read_only,
            timeout=5,
            isolation_level=None,
        )
        self.db.row_factory = sqlite3.Row
        if not read_only:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        if create:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL, strategy TEXT NOT NULL,
                    input_hash TEXT NOT NULL, body TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
                INSERT OR IGNORE INTO metadata VALUES('schema_version', '1');
                INSERT OR IGNORE INTO metadata VALUES('paused', 'false');
                INSERT OR IGNORE INTO metadata VALUES('revoked', 'false');
            """)
        if (
            self.get("schema_version") != "1"
            or self.get("paused") not in ("true", "false")
            or self.get("revoked") not in ("true", "false")
        ):
            self.db.close()
            raise ValueError("unsupported_ledger_schema")

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> DeskStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def record(
        self,
        event_id: str,
        kind: str,
        strategy: str,
        input_hash: str,
        body: dict,
        *,
        require_active: bool = False,
    ) -> tuple[dict, bool]:
        return self.record_batch(
            [
                {
                    "event_id": event_id,
                    "kind": kind,
                    "strategy": strategy,
                    "input_hash": input_hash,
                    "body": body,
                }
            ],
            require_active=require_active,
        )[0]

    def record_batch(
        self,
        events: Iterable[Mapping[str, object]],
        *,
        metadata: Mapping[str, str] | None = None,
        require_active: bool = False,
    ) -> list[tuple[dict, bool]]:
        """Atomically append events and advance their durable projections.

        Existing event ids are idempotent only when both their input hash and body
        match.  Tail validation is constant-time here; callers use ``verify`` at
        startup, restore, and explicit audit boundaries to validate full history.
        """
        pending = list(events)
        results: list[tuple[dict, bool]] = []
        with self.transaction():
            self._verify_tail()
            if require_active and (self.get("paused") == "true" or self.get("revoked") == "true"):
                raise LedgerConflict("operator_halt")
            last = self.db.execute(
                "SELECT seq, event_hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            previous = last["event_hash"] if last else "0" * 64
            last_seq = int(last["seq"]) if last else 0
            for event in pending:
                try:
                    event_id = str(event["event_id"])
                    kind = str(event["kind"])
                    strategy = str(event["strategy"])
                    input_hash = str(event["input_hash"])
                    body = event["body"]
                except KeyError as exc:
                    raise ValueError(f"missing_event_field:{exc.args[0]}") from None
                if not isinstance(body, dict):
                    raise TypeError("event_body_must_be_dict")
                encoded_body = canonical(body)
                old = self.db.execute(
                    "SELECT input_hash, body FROM events WHERE event_id=?", (event_id,)
                ).fetchone()
                if old:
                    if old["input_hash"] != input_hash or old["body"] != encoded_body:
                        raise LedgerConflict("source_id_payload_conflict")
                    results.append((json.loads(old["body"]), False))
                    continue
                material = [previous, event_id, kind, strategy, input_hash, body]
                event_hash = digest(material)
                cursor = self.db.execute(
                    "INSERT INTO events"
                    "(event_id,kind,strategy,input_hash,body,previous_hash,event_hash)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (event_id, kind, strategy, input_hash, encoded_body, previous, event_hash),
                )
                last_seq = int(cursor.lastrowid)
                previous = event_hash
                results.append((body, True))
            for key, value in (metadata or {}).items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise TypeError("metadata_keys_and_values_must_be_strings")
                self.set(key, value)
            self.set("ledger_tip_seq", str(last_seq))
            self.set("ledger_tip_hash", previous)
        return results

    def _verify_tail(self) -> None:
        row = self.db.execute(
            "SELECT seq, event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        actual_seq = int(row["seq"]) if row else 0
        actual_hash = row["event_hash"] if row else "0" * 64
        saved_seq = self.get("ledger_tip_seq")
        saved_hash = self.get("ledger_tip_hash")
        if saved_seq is None and saved_hash is None:
            # Upgrade an older ledger only after validating its complete chain.
            self.verify()
            self.set("ledger_tip_seq", str(actual_seq))
            self.set("ledger_tip_hash", actual_hash)
            return
        if saved_seq != str(actual_seq) or saved_hash != actual_hash:
            raise LedgerConflict("ledger_tail_mismatch")

    def previous(self, event_id: str, input_hash: str) -> dict | None:
        row = self.db.execute(
            "SELECT input_hash, body FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        if row["input_hash"] != input_hash:
            raise LedgerConflict("source_id_payload_conflict")
        return json.loads(row["body"])

    def verify(self) -> int:
        previous = "0" * 64
        count = 0
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            material = [
                previous,
                row["event_id"],
                row["kind"],
                row["strategy"],
                row["input_hash"],
                json.loads(row["body"]),
            ]
            if row["previous_hash"] != previous or row["event_hash"] != digest(material):
                raise LedgerConflict("ledger_integrity_failure")
            previous = row["event_hash"]
            count += 1
        saved_seq = self.get("ledger_tip_seq")
        saved_hash = self.get("ledger_tip_hash")
        if saved_seq is not None and saved_seq != str(count):
            raise LedgerConflict("ledger_tail_mismatch")
        if saved_hash is not None and saved_hash != previous:
            raise LedgerConflict("ledger_tail_mismatch")
        return count

    def journal(self, limit: int = 20) -> list[dict]:
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "strategy": row["strategy"],
                "event_hash": row["event_hash"],
                "result": json.loads(row["body"]),
            }
            for row in self.db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (limit,))
        ]

    def backup(self, target: Path) -> None:
        if target.exists() or target.resolve() == self.path.resolve():
            raise ValueError("backup_target_exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(target) as output:
            self.db.backup(output)
