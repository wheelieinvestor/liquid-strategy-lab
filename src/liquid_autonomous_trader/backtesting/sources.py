"""Causal captured-data capabilities for the unmodified production source paths.

Only in-memory SQLite and supplied immutable events are accessible. There is no
transport factory, credential lookup, DSN, production path or notification sender.
The small account facade projects the simulated ledger; final admission still
runs through the production policy in SimulatedExecution.
"""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS

from liquid_autonomous_trader.backtesting.admission import account_snapshot, utc
from liquid_autonomous_trader.backtesting.events import Event
from liquid_autonomous_trader.cramer_market import CramerMarketData
from liquid_autonomous_trader.cramer_runtime import CramerRuntime
from liquid_autonomous_trader.flow_inputs import CapturedFlowDeliveryV1
from liquid_autonomous_trader.flow_live_source import FlowLiveSource
from liquid_autonomous_trader.gamma_entry_source import GammaEntrySource
from liquid_autonomous_trader.gamma_live_source import GammaLiveSource
from liquid_autonomous_trader.native_perp_source import NativePerpSource


class CapturedInputs:
    def __init__(self):
        self.events: list[Event] = []
        self.now_us = 0
        self.identities = {}

    def clock(self):
        return utc(self.now_us)

    def advance(self, now_us, events=()):
        if now_us < self.now_us:
            raise ValueError("source_clock_regressed")
        self.now_us = now_us
        for event in events:
            if event.available_us > now_us:
                raise ValueError("future_source_event")
            previous = self.identities.get(event.identity)
            if previous is not None:
                if previous != event.content_hash:
                    raise ValueError("conflicting_source_event")
                continue
            self.identities[event.identity] = event.content_hash
            self.events.append(event)

    def rows(self, kind, instrument=None):
        # Revisions replace only their own logical observation after availability.
        latest = {}
        for event in sorted(self.events, key=lambda e: e.order):
            if event.available_us > self.now_us or event.kind != kind:
                continue
            if instrument is not None and event.instrument != instrument:
                continue
            key = (event.source, event.venue, event.instrument, event.event_us, event.sequence)
            old = latest.get(key)
            if old is None or old.revision < event.revision:
                latest[key] = event
        return sorted(latest.values(), key=lambda e: e.order)

    def latest(self, kind, instrument=None, *, maximum_age_us=None):
        rows = self.rows(kind, instrument)
        if not rows:
            raise ValueError("missing_" + kind)
        event = rows[-1]
        if maximum_age_us is not None and self.now_us - event.event_us > maximum_age_us:
            raise ValueError("stale_" + kind)
        if event.quality == "unsupported":
            raise ValueError("unsupported_" + kind)
        return event

    def call_read_tool(self, name, arguments):
        if name != "analyze_market" or set(arguments) != {"symbol"}:
            raise ValueError("research_read_capability_unavailable")
        event = self.latest("market_identity", arguments["symbol"], maximum_age_us=300_000_000)
        return NS(is_error=False, structured_content=event.payload)

    def fetch(self, request):
        kind = request.get("type")
        if kind == "meta":
            event = self.latest(
                "native_metadata", request.get("dex") or "native", maximum_age_us=300_000_000
            )
        elif kind == "l2Book":
            event = self.latest("native_book", request["coin"], maximum_age_us=5_000_000)
        elif kind == "candleSnapshot" and request["req"].get("interval") == "15m":
            event = self.latest(
                "native_candles15m", request["req"]["coin"], maximum_age_us=900_000_000
            )
            # In-flight bars may occur in captured responses. The production
            # validators select closed bars; no later response is ever supplied.
        else:
            raise ValueError("research_public_read_unavailable")
        return event.payload

    def read(self, now):
        if now != self.clock():
            raise ValueError("gamma_clock_mismatch")
        result = {}
        for kind in ("matrix", "spot"):
            event = self.latest("gamma_" + kind, "QQQ", maximum_age_us=300_000_000)
            result[kind] = {
                "result": event.payload,
                "raw_sha256": event.content_hash,
                "fetched_at": utc(event.available_us),
            }
        return result

    def classifications(self, after):
        return [
            {"seq": e.sequence, **e.payload}
            for e in self.rows("cramer_classification")
            if e.sequence > after
        ]


class ResearchAccount:
    """Account/lifecycle-shaped read projection and simulated command collector.

    These tables are adapter state, not an alternative monetary ledger. They
    preserve the exact production epoch/cooldown queries across replay/restarts.
    """

    def __init__(self, simulation, inputs):
        self.sim, self.inputs = simulation, inputs
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE executions(intent_id TEXT PRIMARY KEY,strategy TEXT,symbol TEXT,
              side TEXT,state TEXT,order_id TEXT,filled_quantity TEXT,protected_quantity TEXT,
              initial_stop TEXT);
            CREATE TABLE lifecycle(seq INTEGER PRIMARY KEY,intent_id TEXT,state TEXT,
              detail TEXT,at TEXT);
            CREATE TABLE liquid_market_owners(symbol TEXT PRIMARY KEY,owner_intent TEXT);
            CREATE TABLE liquid_operations(operation_id TEXT PRIMARY KEY,owner_intent TEXT,
              kind TEXT,state TEXT,started_at TEXT);
            CREATE TABLE sleeve_runtime_decisions(intent_id TEXT PRIMARY KEY,plan TEXT);
            CREATE TABLE btc_runtime_decisions(intent_id TEXT PRIMARY KEY,plan TEXT);
        """)
        self.risk_store = NS(db=self.db, unresolved=self.executions, execution=self.execution)
        self.journal = NS(db=self.db, unresolved=self.operations, operation=self.operation)
        self.reconciler = NS(
            client=inputs,
            position=self.position,
            accounting_projection=NS(conservative_loss=D(0)),
            before_account=None,
        )
        self.commands = []
        self.cramer_symbols = set()

    def risk_projection(self, *args, **kwargs):
        return account_snapshot(self.sim.ledger, self.inputs.now_us)

    def executions(self):
        return self.db.execute("SELECT * FROM executions WHERE state!='closed'").fetchall()

    def execution(self, owner):
        return self.db.execute("SELECT * FROM executions WHERE intent_id=?", (owner,)).fetchone()

    def operations(self):
        return self.db.execute("SELECT * FROM liquid_operations WHERE state='unknown'").fetchall()

    def operation(self, key):
        return self.db.execute(
            "SELECT * FROM liquid_operations WHERE operation_id=?", (key,)
        ).fetchone()

    def sync(self):
        self.db.execute("DELETE FROM liquid_market_owners")
        for key, order in self.sim.orders.items():
            p = self.sim.ledger.positions.get(order.symbol)
            owns = p is not None and p.owner == order.owner
            # A later entry in the same strategy must not revive an older owner.
            later = any(
                o.symbol == order.symbol and o.submitted_us > order.submitted_us
                for o in self.sim.orders.values()
                if o.fills
            )
            owns = owns and not later
            active = order.state in {"pending", "partial", "unknown", "acknowledged"}
            state = "open" if owns else order.state if active else "closed"
            quantity = abs(p.quantity) if owns else D(0)
            existing = self.execution(key)
            self.db.execute(
                "INSERT OR REPLACE INTO executions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    order.owner,
                    order.symbol,
                    "long" if order.signed_quantity > 0 else "short",
                    state,
                    key,
                    str(quantity),
                    str(quantity if owns and p.stop is not None else 0),
                    str(order.stop),
                ),
            )
            if existing is None:
                self.db.execute(
                    "INSERT INTO lifecycle(intent_id,state,detail,at) VALUES(?,?,?,?)",
                    (key, "reserved", "{}", utc(order.submitted_us).isoformat()),
                )
            if owns or active:
                self.db.execute("INSERT INTO liquid_market_owners VALUES(?,?)", (order.symbol, key))
            self.db.execute(
                "INSERT OR REPLACE INTO liquid_operations VALUES(?,?,?,?,?)",
                (
                    key,
                    key,
                    "entry",
                    "unknown" if order.state == "unknown" else "acknowledged",
                    utc(order.submitted_us).isoformat(),
                ),
            )
        for key, command in self.sim.management.items():
            self.db.execute(
                "INSERT OR REPLACE INTO liquid_operations VALUES(?,?,?,?,?)",
                (
                    key,
                    command["owner_order"],
                    command["action"],
                    "retired"
                    if command["state"] in {"applied", "flat_reconciled", "rejected"}
                    else "unknown",
                    utc(command["submitted_us"]).isoformat(),
                ),
            )

    def _position(self, symbol):
        p = self.sim.ledger.positions.get(symbol)
        if p is None:
            return None
        return NS(
            broker_symbol=symbol,
            side="long" if p.quantity > 0 else "short",
            quantity=abs(p.quantity),
            entry_price=p.entry,
            mark_price=self.sim.ledger.marks[symbol],
            stop_price=p.stop,
            leverage=p.leverage,
            margin_used_usd=self.sim.ledger.margin(p),
        )

    def position(self, symbol, *, all_positions=False):
        positions = [self._position(s) for s in self.sim.ledger.positions]
        account = NS(
            positions=positions,
            received_at=self.inputs.clock(),
            equity_usd=self.sim.ledger.equity(),
            route=NS(value="paper"),
        )
        p = self._position(symbol)
        working = tuple(
            o.order_id
            for o in self.sim.orders.values()
            if o.symbol == symbol and o.state in {"pending", "partial", "unknown", "acknowledged"}
        )
        evidence = NS(
            position=p,
            working_order_ids=working,
            protection_verified=p is not None and p.stop_price is not None,
        )
        return account, NS(working_snapshot_complete=True), evidence

    def reconcile(self, owner):
        self.sync()
        record = self.execution(owner)
        if record is None:
            raise ValueError("research_owner_missing")
        return self.position(record["symbol"])[2]

    def _owned(self, owner):
        record = self.execution(owner)
        a, o, evidence = self.position(record["symbol"])
        return record, a, o, evidence

    def register_cramer_market(self, identity):
        self.cramer_symbols.add(identity.coin)

    def close_owned(self, owner, *, request_id):
        self.commands.append({"action": "close", "owner": owner, "request_id": request_id})

    def tighten_stop(self, owner, *, request_id, stop):
        self.commands.append(
            {
                "action": "stop",
                "owner": owner,
                "request_id": request_id,
                "stop": str(stop),
                "allow_loosen": False,
            }
        )

    def reduce_owned(self, owner, *, request_id, quantity, quantity_step):
        self.commands.append(
            {
                "action": "reduce",
                "owner": owner,
                "request_id": request_id,
                "quantity": str(quantity),
            }
        )

    def close(self):
        self.db.close()


class SourceAdapters:
    def __init__(self, simulation, inputs=None):
        self.inputs = inputs or CapturedInputs()
        self.account = ResearchAccount(simulation, self.inputs)
        clock = self.inputs.clock
        native = NativePerpSource(fetch=self.inputs.fetch, clock=clock)
        self.flow = FlowLiveSource(
            self.account,
            native,
            path="research-unused",
            clock=clock,
            capture=self._flow_capture,
            risk_projection=self.account.risk_projection,
        )
        self.gamma_source = GammaLiveSource(
            self.account.db, self.inputs, native, self.inputs, clock=clock
        )
        self.gamma_source.consumed = lambda key: key in simulation.seen_signals
        self.gamma = GammaEntrySource(
            self.gamma_source,
            self.account,
            clock=clock,
            risk_projection=self.account.risk_projection,
        )
        market = CramerMarketData(self.inputs, native, clock=clock)
        self.cramer = CramerRuntime(
            self.account,
            market,
            NS(fetch=self.inputs.classifications),
            clock=clock,
            risk_projection=self.account.risk_projection,
        )

    def _flow_capture(self, now):
        cursor = self.account.db.execute(
            "SELECT seq FROM sleeve_source_cursors WHERE name='flow'"
        ).fetchone()[0]
        for event in self.inputs.rows("flow_delivery"):
            if event.sequence <= cursor:
                continue
            captured = CapturedFlowDeliveryV1.model_validate(event.payload)
            if not captured.signal.delivered_at <= captured.captured_at <= now:
                raise ValueError("noncausal_flow_capture")
            if captured.captured_at > utc(event.available_us):
                raise ValueError("flow_arrival_before_capture")
            if (now - captured.signal.delivered_at).total_seconds() > 300:
                self.flow.acknowledge(event.sequence)
                continue
            return captured, event.sequence
        return None, None

    def source_state(self):
        """Data-only checkpoint; never restore executable SQL from an input file."""
        tables = [
            r[0]
            for r in self.account.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name!='sqlite_sequence' ORDER BY name"
            )
        ]
        return {
            name: [dict(r) for r in self.account.db.execute('SELECT * FROM "' + name + '"')]
            for name in tables
        }

    def restore_source_state(self, state):
        expected = self.source_state()
        if set(state) != set(expected):
            raise ValueError("adapter_checkpoint_tables_mismatch")
        for name, rows in state.items():
            columns = [r[1] for r in self.account.db.execute('PRAGMA table_info("' + name + '")')]
            if any(set(row) != set(columns) for row in rows):
                raise ValueError("adapter_checkpoint_columns_mismatch")
        with self.account.db:
            for name, rows in state.items():
                self.account.db.execute('DELETE FROM "' + name + '"')
                for row in rows:
                    columns = list(row)
                    sql = (
                        'INSERT INTO "'
                        + name
                        + '" ('
                        + ",".join('"' + c + '"' for c in columns)
                        + ") VALUES ("
                        + ",".join("?" for c in columns)
                        + ")"
                    )
                    self.account.db.execute(sql, [deepcopy(row[c]) for c in columns])

    def close(self):
        self.account.close()

    def gamma_state(self):
        def encode(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, D):
                return str(value)
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [encode(item) for item in value]
            return value

        gamma = self.gamma_source
        return encode(
            {
                "identity": asdict(gamma.identity) if gamma.identity else None,
                "metadata": gamma.metadata,
                "quotes": [asdict(q) for q in gamma.current_quotes],
                "bars": [asdict(gamma.bars[0]), gamma.bars[1]] if gamma.bars else None,
                "exit_context": gamma.latest_exit_context,
            }
        )

    def restore_gamma_state(self, state):
        from liquid_autonomous_trader.native_perp_source import NativeMarketIdentity, NativeQuote
        from liquid_autonomous_trader.xyz_bars import XYZBar, XYZBarProof

        def identity(value):
            return NativeMarketIdentity(
                **{
                    **value,
                    "liquid_max_leverage": D(value["liquid_max_leverage"]),
                    "resolved_at": datetime.fromisoformat(value["resolved_at"]),
                }
            )

        gamma = self.gamma_source
        gamma.identity = identity(state["identity"]) if state["identity"] else None
        gamma.metadata, gamma.latest_exit_context = (
            deepcopy(state["metadata"]),
            deepcopy(state["exit_context"]),
        )
        gamma.current_quotes.clear()
        for original in state["quotes"]:
            row = dict(original)
            row["identity"] = identity(row["identity"])
            for key in ("observed_at", "received_at"):
                row[key] = datetime.fromisoformat(row[key])
            for key in (
                "bid",
                "ask",
                "quantity_step",
                "maximum_leverage",
                "bid_notional",
                "ask_notional",
                "deployer_fee_scale",
            ):
                row[key] = D(row[key])
            gamma.current_quotes.append(NativeQuote(**row))
        if state["bars"]:
            proof, atr = deepcopy(state["bars"])
            for key in ("prior", "latest", "one_hour_anchor"):
                bar = proof[key]
                for stamp in ("start_at", "end_at"):
                    bar[stamp] = datetime.fromisoformat(bar[stamp])
                for field in ("open", "high", "low", "close", "volume"):
                    bar[field] = D(bar[field])
                proof[key] = XYZBar(**bar)
            proof["one_hour_return"] = D(proof["one_hour_return"])
            gamma.bars = XYZBarProof(**proof), D(atr)
