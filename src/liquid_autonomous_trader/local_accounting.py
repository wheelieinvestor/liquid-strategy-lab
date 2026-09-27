"""Private append-only accounting from Liquid observations, not fabricated fills.

Negative changes in equity less unrealized P&L remain recorded as conservative
cash debits. Positive changes never erase that history. Individual commissions,
funding payments and external transfers cannot be separated from these snapshots.
No deposits/withdrawals may occur while this accounting mode admits entries.
"""

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from liquid_autonomous_trader.computer_adapter import ComputerRoute, require_account_binding


class AccountingError(ValueError):
    pass


def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class AccountingProjection:
    day: str
    observed_at: datetime
    opening_equity: Decimal
    current_equity: Decimal
    conservative_loss: Decimal
    blockers: tuple[str, ...]
    basis: str = "local_conservative_cash_debits"

    def flat_risk_snapshot(self, account, orders):
        """BTC-first admission inputs when the entire account is verified flat.

        The execution store replaces local reservation counts under its lock.
        This is not a general multi-strategy ownership projection.
        """
        from liquid_autonomous_trader.live_policy import AccountRiskSnapshot

        if (
            account.positions
            or account.margin_used_usd
            or orders.orders
            or not orders.working_snapshot_complete
        ):
            raise AccountingError("accounting_flat_account_required")
        if not 0 <= (self.observed_at - orders.received_at).total_seconds() <= 5:
            raise AccountingError("accounting_order_snapshot_stale")
        zero = Decimal(0)
        return self.apply(
            AccountRiskSnapshot(
                opening_equity=self.opening_equity,
                current_equity=account.equity_usd,
                actual_available_collateral=account.available_collateral_usd,
                owned_open_collateral=zero,
                foreign_open_collateral=zero,
                pending_collateral=zero,
                unknown_collateral=zero,
                durable_reservations=zero,
                positions_by_strategy={},
                collateral_by_strategy={},
                realized_pnl=None,
                fees=None,
                funding=None,
                open_stop_risk=zero,
                pending_unknown_stop_risk=zero,
                durable_reserved_stop_risk=zero,
                flow_epoch_risk_including_costs=zero,
                reconciled=True,
                observed_at=account.received_at,
            )
        )

    def apply(self, snapshot):
        if self.blockers:
            raise AccountingError(",".join(self.blockers))
        if (
            snapshot.observed_at != self.observed_at
            or snapshot.current_equity != self.current_equity
        ):
            raise AccountingError("accounting_snapshot_mismatch")
        return replace(
            snapshot,
            opening_equity=self.opening_equity,
            realized_pnl=None,
            fees=None,
            funding=None,
            accounted_loss_usd=self.conservative_loss,
            accounting_basis=self.basis,
        )


class LocalAccountingLedger:
    def __init__(self, path: Path, *, route=ComputerRoute.LIVE, max_gap_seconds=90):
        if not path.is_absolute() or path.is_symlink() or not isinstance(route, ComputerRoute):
            raise AccountingError("accounting_path_or_route_invalid")
        if not 5 <= max_gap_seconds <= 90:
            raise AccountingError("accounting_gap_limit_invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise AccountingError("accounting_parent_symlink")
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise AccountingError("accounting_file_permissions_invalid")
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounting_identity(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), route TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounting_observations(
                seq INTEGER PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS accounting_no_update
                BEFORE UPDATE ON accounting_observations
                BEGIN SELECT RAISE(ABORT, 'append_only'); END;
            CREATE TRIGGER IF NOT EXISTS accounting_no_delete
                BEFORE DELETE ON accounting_observations
                BEGIN SELECT RAISE(ABORT, 'append_only'); END;
        """)
        self.db.execute("INSERT OR IGNORE INTO accounting_identity VALUES(1,?)", (route.value,))
        if self.db.execute("SELECT route FROM accounting_identity").fetchone()[0] != route.value:
            self.db.close()
            raise AccountingError("accounting_route_mismatch")
        self.route, self.max_gap_seconds = route, max_gap_seconds
        try:
            self.verify()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def verify(self):
        previous = "0" * 64
        count = 0
        for row in self.db.execute("SELECT * FROM accounting_observations ORDER BY seq"):
            count += 1
            expected = hashlib.sha256(
                canonical([previous, row["event_id"], row["body"]]).encode()
            ).hexdigest()
            if (
                row["seq"] != count
                or row["previous_hash"] != previous
                or row["event_hash"] != expected
            ):
                raise AccountingError("accounting_log_integrity_failed")
            previous = expected
        return count

    def observe(self, account, *, outstanding=(), activity_ids=(), working_order_count=0, now=None):
        now = now or datetime.now(UTC)
        require_account_binding(
            account, expected_username="research-account", expected_route=self.route, now=now
        )
        unrealized = sum((p.unrealized_pnl_usd for p in account.positions), Decimal(0))
        equity = account.equity_usd
        if not equity.is_finite() or equity < 0 or not unrealized.is_finite():
            raise AccountingError("accounting_nonpositive_or_invalid_equity")
        instant = account.received_at.astimezone(UTC)
        event_id = instant.isoformat()
        observation = {
            "observed_at": event_id,
            "equity": str(equity),
            "cash_component": str(equity - unrealized),
            "unrealized_pnl": str(unrealized),
            "margin": str(account.margin_used_usd),
            "position_count": len(account.positions),
            "outstanding": sorted(outstanding),
            "activity_ids": sorted(activity_ids),
            "working_order_count": working_order_count,
        }
        self.db.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self.db.execute(
                "SELECT body FROM accounting_observations WHERE event_id=?", (event_id,)
            ).fetchone()
            if duplicate:
                body = json.loads(duplicate[0])
                if body["observation"] != observation:
                    raise AccountingError("accounting_event_conflict")
                self.db.execute("COMMIT")
                return self._projection(body)
            last = self.db.execute(
                "SELECT * FROM accounting_observations ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prior = json.loads(last["body"]) if last else None
            blockers = list(prior["persistent_blockers"]) if prior else []
            day = instant.date().isoformat()
            opening, debits, maximum = equity, Decimal(0), Decimal(0)
            if prior:
                previous = prior["observation"]
                elapsed = (
                    instant - datetime.fromisoformat(previous["observed_at"])
                ).total_seconds()
                if elapsed <= 0:
                    raise AccountingError("accounting_time_regression")
                if elapsed > self.max_gap_seconds:
                    blockers.append("accounting_observation_gap")
                if (
                    not previous["position_count"]
                    and not account.positions
                    and equity - unrealized > Decimal(previous["cash_component"])
                    and observation["activity_ids"] == previous["activity_ids"]
                ):
                    blockers.append("accounting_unexplained_flat_credit")
                if day == prior["day"]:
                    opening = Decimal(prior["opening_equity"])
                    debits, maximum = Decimal(prior["cash_debits"]), Decimal(prior["loss"])
                else:
                    # Carry the immediately preceding observation across midnight;
                    # never create extra capacity by discarding the boundary interval.
                    opening = Decimal(previous["equity"])
                debits += max(
                    Decimal(0), Decimal(previous["cash_component"]) - (equity - unrealized)
                )
            elif (
                equity == 0
                or account.positions
                or account.margin_used_usd
                or outstanding
                or working_order_count
            ):
                blockers.append("accounting_clean_bootstrap_required")
            persistent = sorted(set(blockers))
            if outstanding:
                blockers.append("accounting_unresolved_operations")
            loss = max(maximum, debits, opening - equity, Decimal(0))
            body = {
                "observation": observation,
                "day": day,
                "opening_equity": str(opening),
                "cash_debits": str(debits),
                "loss": str(loss),
                "persistent_blockers": persistent,
                "blockers": sorted(set(blockers)),
            }
            encoded = canonical(body)
            previous_hash = last["event_hash"] if last else "0" * 64
            event_hash = hashlib.sha256(
                canonical([previous_hash, event_id, encoded]).encode()
            ).hexdigest()
            self.db.execute(
                "INSERT INTO accounting_observations VALUES(?,?,?,?,?)",
                ((last["seq"] + 1) if last else 1, event_id, encoded, previous_hash, event_hash),
            )
            self.db.execute("COMMIT")
            return self._projection(body)
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _projection(body):
        return AccountingProjection(
            body["day"],
            datetime.fromisoformat(body["observation"]["observed_at"]),
            Decimal(body["opening_equity"]),
            Decimal(body["observation"]["equity"]),
            Decimal(body["loss"]),
            tuple(body["blockers"]),
        )

    def recover_pre_entry_gap(self, account, orders, *, executions, journal, now):
        """Append a narrow recovery proof, never erase observations or loss.

        Caller holds the executor process lock and has disabled entries. This is
        valid only before this executor has EVER reserved or dispatched an order,
        under the standing exclusive-bot-account/no-unreconciled-transfer mandate.
        It does not infer fills or repair a gap after trading has begun.
        """
        require_account_binding(
            account, expected_username="research-account", expected_route=self.route, now=now
        )
        if journal.route != self.route or account.positions or account.margin_used_usd:
            raise AccountingError("pre_entry_recovery_requires_flat_account")
        if (
            orders.orders
            or not orders.working_snapshot_complete
            or not 0 <= (now - orders.received_at).total_seconds() <= 5
        ):
            raise AccountingError("pre_entry_recovery_requires_fresh_empty_orders")
        if (
            executions.db.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
            or journal.db.execute("SELECT COUNT(*) FROM liquid_operations").fetchone()[0]
            or journal.db.execute("SELECT COUNT(*) FROM liquid_market_owners").fetchone()[0]
        ):
            raise AccountingError("pre_entry_recovery_has_execution_history")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.verify()
            rows = self.db.execute("SELECT * FROM accounting_observations ORDER BY seq").fetchall()
            if not rows:
                raise AccountingError("pre_entry_recovery_history_required")
            last = rows[-1]
            body = json.loads(last["body"])
            if body["blockers"] != ["accounting_observation_gap"]:
                raise AccountingError("pre_entry_recovery_only_observation_gap")
            if (
                body["observation"]["observed_at"]
                != account.received_at.astimezone(UTC).isoformat()
            ):
                raise AccountingError("pre_entry_recovery_snapshot_mismatch")
            for row in rows:
                prior = json.loads(row["body"])
                obs = prior["observation"]
                if (
                    obs["position_count"]
                    or obs["working_order_count"]
                    or obs["outstanding"]
                    or obs["activity_ids"]
                    or Decimal(obs["margin"]) != 0
                    or Decimal(obs["equity"]) != account.equity_usd
                    or Decimal(obs["cash_component"]) != account.equity_usd
                    or Decimal(prior["loss"]) != 0
                    or Decimal(prior["cash_debits"]) != 0
                    or set(prior["blockers"]) - {"accounting_observation_gap"}
                ):
                    raise AccountingError("pre_entry_recovery_history_not_clean")
            # Original gap observations remain in the verified hash chain.
            # No budget fields or timestamps are changed.
            body["blockers"] = []
            body["persistent_blockers"] = []
            body["recovery"] = {
                "kind": "pre_first_entry_flat_gap",
                "prior_event_hash": last["event_hash"],
                "verified_observations": len(rows),
                "ever_executions": 0,
                "ever_operations": 0,
                "assumption": "exclusive_bot_account_no_unreconciled_transfers",
            }
            event_id = "pre-entry-gap-recovery:" + last["event_hash"]
            encoded = canonical(body)
            digest = hashlib.sha256(
                canonical([last["event_hash"], event_id, encoded]).encode()
            ).hexdigest()
            self.db.execute(
                "INSERT INTO accounting_observations VALUES(?,?,?,?,?)",
                (last["seq"] + 1, event_id, encoded, last["event_hash"], digest),
            )
            self.db.execute("COMMIT")
            return self._projection(body)
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
