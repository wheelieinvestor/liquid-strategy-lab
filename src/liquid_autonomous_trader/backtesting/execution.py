"""Simulated asynchronous transport with deterministic, event-driven matching.

No live transports, credentials or notification senders are accepted. Native stops
are independent of application availability. Unknown outcomes retain reservations
until an explicit simulated venue reconciliation arrives.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from liquid_autonomous_trader.backtesting.admission import admit
from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.ledger import (
    FeeSchedule,
    Instrument,
    Ledger,
    Tier,
    number,
    serial,
)
from liquid_autonomous_trader.live_policy import EntryRiskRequest


@dataclass
class Order:
    order_id: str
    symbol: str
    owner: str
    signed_quantity: D
    remaining: D
    stop: D
    target: D | None
    leverage: D
    mode: str
    submitted_us: int
    executable_us: int
    acknowledged_us: int
    expires_us: int
    kind: str
    limit: D | None
    state: str
    cancel_us: int | None = None
    fills: int = 0


class SimulatedExecution:
    def __init__(self, ledger: Ledger, fees: FeeSchedule, *, adverse_intrabar=True):
        self.ledger, self.fees = ledger, fees
        self.adverse_intrabar = adverse_intrabar
        self.orders: dict[str, Order] = {}
        self.decisions: list[dict] = []
        self.events: list[dict] = []
        self.targets: dict[str, D] = {}
        self.seen_signals: dict[str, str] = {}
        self.halted = False
        self.last_us = 0
        self.triggered_stops: set[str] = set()
        self.seen_quotes: dict[str, str] = {}
        self.management: dict[str, dict] = {}

    def submit(
        self,
        signal_id: str,
        request: EntryRiskRequest,
        now_us: int,
        *,
        mode: str,
        expires_us: int,
        target=None,
        latency_us=1,
        ack_delay_us=0,
        fault=None,
        kind="market",
        limit=None,
    ):
        material = serial(
            {
                "request": asdict(request),
                "mode": mode,
                "expires_us": expires_us,
                "target": target,
                "latency_us": latency_us,
                "ack_delay_us": ack_delay_us,
                "fault": fault,
                "kind": kind,
                "limit": limit,
            }
        )
        fingerprint = digest(material)
        if signal_id in self.seen_signals:
            if self.seen_signals[signal_id] != fingerprint:
                raise ValueError("signal_identity_conflict")
            return "duplicate"
        self.seen_signals[signal_id] = fingerprint
        reason = None
        try:
            if self.halted:
                raise ValueError("halted")
            if any(m["state"] == "unknown" for m in self.management.values()):
                raise ValueError("prior_management_outcome_unresolved")
            if now_us < self.last_us or expires_us <= now_us:
                raise ValueError("stale_signal")
            if mode not in {"cross", "isolated"} or kind not in {"market", "limit"}:
                raise ValueError("unsupported_order")
            if latency_us < 1 or ack_delay_us < 0:
                raise ValueError("positive_latency_required")
            if kind == "limit" and (limit is None or number(limit, positive=True) <= 0):
                raise ValueError("limit_price_required")
            if target is not None:
                number(target, positive=True)
            admission = admit(self.ledger, request, now_us)
            if fault in {"reject", "rate_limit"}:
                raise ValueError(fault)
            self.ledger.apply(
                f"reserve:{signal_id}",
                now_us,
                "reserve",
                order_id=signal_id,
                symbol=request.symbol,
                owner=request.strategy.value,
                collateral=admission.reserved_collateral,
                state="unknown" if fault == "unknown" else "pending",
            )
            quantity = admission.quantity * (1 if request.side == "long" else -1)
            self.orders[signal_id] = Order(
                signal_id,
                request.symbol,
                request.strategy.value,
                quantity,
                abs(quantity),
                admission.widened_initial_stop,
                number(target, positive=True) if target is not None else None,
                request.selected_leverage,
                mode,
                now_us,
                now_us + latency_us,
                now_us + ack_delay_us,
                expires_us,
                kind,
                number(limit, positive=True) if limit is not None else None,
                "unknown" if fault == "unknown" else "pending",
            )
            reason = "unknown" if fault == "unknown" else "accepted"
        except ValueError as error:
            reason = str(error)
        self.decisions.append(
            {
                "signal_id": signal_id,
                "at_us": now_us,
                "reason": reason,
                "request_hash": fingerprint,
                "strategy": request.strategy.value,
            }
        )
        return reason

    def cancel(self, order_id, now_us, *, latency_us=1):
        order = self.orders[order_id]
        if order.state == "unknown":
            self.events.append(
                {"kind": "cancel_blocked_unknown", "order": order_id, "at_us": now_us}
            )
            return
        if latency_us < 1:
            raise ValueError("positive_latency_required")
        order.cancel_us = now_us + latency_us

    def manage(
        self,
        request_id,
        owner_order,
        action,
        now_us,
        *,
        stop=None,
        quantity=None,
        allow_loosen=False,
        latency_us=1,
        fault=None,
    ):
        """Queue a software exit/stop replacement; old native protection remains.

        Unknown writes require explicit simulated reconciliation. A later market
        observation supplies executable depth; a decision cannot fill itself.
        """
        if action not in {"close", "reduce", "stop"} or latency_us < 1:
            raise ValueError("invalid_management_command")
        order = self.orders[owner_order]
        command = serial(
            dict(
                owner_order=owner_order,
                action=action,
                submitted_us=now_us,
                executable_us=now_us + latency_us,
                stop=stop,
                quantity=quantity,
                allow_loosen=allow_loosen,
                fault=fault,
            )
        )
        if request_id in self.management:
            old = self.management[request_id]
            if old["command_hash"] != digest(command):
                raise ValueError("management_identity_conflict")
            return "duplicate"
        p = self.ledger.positions.get(order.symbol)
        if p is None or p.owner != order.owner:
            raise ValueError("management_owner_not_open")
        if now_us < self.last_us:
            raise ValueError("management_clock_regressed")
        if any(
            m["owner_order"] == owner_order and m["state"] in {"pending", "unknown"}
            for m in self.management.values()
        ):
            return "management_pending"
        if action == "stop":
            number(stop, positive=True)
        if action == "reduce" and not 0 < number(quantity, positive=True) < abs(p.quantity):
            raise ValueError("invalid_reduce_quantity")
        self.management[request_id] = {
            **command,
            "command_hash": digest(command),
            "state": "unknown"
            if fault == "unknown"
            else "rejected"
            if fault == "reject"
            else "pending",
            "remaining": str(abs(p.quantity) if quantity is None else quantity),
            "before_quantity": str(p.quantity),
            "before_entry": str(p.entry),
            "before_stop": str(p.stop),
        }
        return self.management[request_id]["state"]

    def reconcile_management(
        self, request_id, now_us, *, applied=False, terminal_rejected=False, known_pending=False
    ):
        command = self.management[request_id]
        if (
            command["state"] != "unknown"
            or now_us < max(command["submitted_us"], self.ledger.last_us)
            or sum((applied, terminal_rejected, known_pending)) != 1
        ):
            raise ValueError("invalid_management_reconciliation")
        # A claimed application is not a fill. The injected venue execution must
        # already be reflected in the ledger, then its state is verified here.
        order = self.orders[command["owner_order"]]
        p = self.ledger.positions.get(order.symbol)
        if any(
            o.symbol == order.symbol and o.fills and o.submitted_us > order.submitted_us
            for o in self.orders.values()
        ):
            raise ValueError("management_owner_changed")
        if p is not None and (p.owner != order.owner or p.entry != D(command["before_entry"])):
            raise ValueError("management_position_changed")
        if applied:
            if command["action"] == "stop":
                if p is None or p.stop != D(command["stop"]):
                    raise ValueError("stop_application_not_observed")
            elif command["action"] == "reduce":
                before = D(command["before_quantity"])
                expected = before - D(command["quantity"]) * (1 if before > 0 else -1)
                if p is None or p.quantity != expected:
                    raise ValueError("reduction_application_not_observed")
            elif p is not None:
                raise ValueError("exit_application_not_observed")
            command["state"] = "applied"
            command["remaining"] = "0"
        elif terminal_rejected:
            command["state"] = "rejected"
        else:
            if (
                p is None
                or p.quantity != D(command["before_quantity"])
                or p.stop != D(command["before_stop"])
            ):
                raise ValueError("unknown_partial_or_intervening_activity")
            command["state"] = "pending"
            command["executable_us"] = max(command["executable_us"], now_us + 1)

    def _management_quote(self, symbol, at_us, buy, sell):
        for key, command in self.management.items():
            order = self.orders[command["owner_order"]]
            if order.symbol != symbol or command["state"] != "pending":
                continue
            if at_us < command["executable_us"]:
                continue
            p = self.ledger.positions.get(symbol)
            if p is None:
                command["state"] = "flat_reconciled"
                continue
            if p.owner != order.owner or any(
                o.symbol == symbol and o.fills and o.submitted_us > order.submitted_us
                for o in self.orders.values()
            ):
                command["state"] = "owner_changed"
                continue
            if command["action"] == "stop":
                try:
                    self.ledger.apply(
                        "manage:" + key,
                        at_us,
                        "stop",
                        symbol=symbol,
                        owner=order.owner,
                        price=command["stop"],
                        allow_loosen=command["allow_loosen"],
                    )
                    command["state"] = "applied"
                except ValueError as error:
                    command["state"] = "rejected"
                    command["reason"] = str(error)
                continue
            levels = sell if p.quantity > 0 else buy
            step = self.ledger.instruments[symbol].quantity_step
            for index, level in enumerate(levels):
                if symbol not in self.ledger.positions or D(command["remaining"]) <= 0:
                    break
                quantity = (
                    min(
                        abs(self.ledger.positions[symbol].quantity),
                        level[1],
                        D(command["remaining"]),
                    )
                    / step
                ).to_integral_value(rounding=ROUND_FLOOR) * step
                if not quantity:
                    continue
                self._close(symbol, level[0], at_us, "software:" + key + ":" + str(index), quantity)
                level[1] -= quantity
                command["remaining"] = str(D(command["remaining"]) - quantity)
            if symbol not in self.ledger.positions or D(command["remaining"]) == 0:
                command["state"] = "applied"

    def reconcile_unknown(self, order_id, now_us, *, terminal_no_fill: bool):
        order = self.orders[order_id]
        if order.state != "unknown":
            raise ValueError("not_an_unknown_order")
        if terminal_no_fill:
            self._finish(order, now_us, "rejected")
        else:
            # Outcome is now known pending; never resubmit the original signal.
            order.state = "pending"
            self.ledger.apply(f"resolve:{order_id}", now_us, "resolve", order_id=order_id)
        self.events.append(
            {
                "kind": "reconciliation",
                "order": order_id,
                "at_us": now_us,
                "terminal_no_fill": terminal_no_fill,
            }
        )

    def _finish(self, order, at_us, state):
        order.state = state
        if order.order_id in self.ledger.reservations:
            self.ledger.apply(
                f"release:{order.order_id}:{state}",
                at_us,
                "release",
                order_id=order.order_id,
                outcome_known=True,
            )

    def _close(self, symbol, price, at_us, cause, quantity_limit=None, *, reference_price=None):
        position = self.ledger.positions[symbol]
        spec = self.ledger.instruments[symbol]
        price = (price / spec.price_step).to_integral_value(
            rounding=ROUND_FLOOR if position.quantity > 0 else ROUND_CEILING
        ) * spec.price_step
        quantity = -position.quantity
        if quantity_limit is not None:
            quantity = min(abs(quantity), quantity_limit) * (1 if quantity > 0 else -1)
        fee = self.fees.charge(abs(quantity * price), at_us, False)
        event_id = f"close:{symbol}:{at_us}:{cause}"
        self.ledger.apply(
            event_id,
            at_us,
            "fill",
            symbol=symbol,
            owner=position.owner,
            quantity=quantity,
            price=price,
            fee=fee,
            leverage=position.leverage,
            mode=position.mode,
            reduce_only=True,
            reference_price=self.ledger.marks[symbol]
            if reference_price is None
            else reference_price,
        )
        if symbol not in self.ledger.positions:
            self.targets.pop(symbol, None)
            self.triggered_stops.discard(symbol)
        for order in self.orders.values():
            if order.symbol == symbol and order.state in {"pending", "acknowledged", "partial"}:
                self._finish(order, at_us, "cancelled_after_exit")
        self.events.append(
            {"kind": "exit", "symbol": symbol, "at_us": at_us, "cause": cause, "price": str(price)}
        )

    def quote(self, symbol, at_us, *, bids, asks, mark=None, synthetic_locked=False):
        """Consume observed depth once per observation, in submission order.

        A limit fills only on a strictly marketable level; touching/resting queues
        are unsupported and not assumed filled. Unknown orders intentionally do not
        resolve from market quotes. Tests inject an authoritative outcome instead.
        """
        quote_key = f"{symbol}:{at_us}"
        fingerprint = digest(serial({"bids": bids, "asks": asks, "mark": mark}))
        if quote_key in self.seen_quotes:
            if self.seen_quotes[quote_key] != fingerprint:
                raise ValueError("conflicting_quote_revision")
            return
        if at_us < self.last_us:
            raise ValueError("execution_clock_regressed")
        buy = [[number(p, positive=True), number(q, nonnegative=True)] for p, q in asks]
        sell = [[number(p, positive=True), number(q, nonnegative=True)] for p, q in bids]
        if (
            not buy
            or not sell
            or sell[0][0] > buy[0][0]
            or (sell[0][0] == buy[0][0] and not synthetic_locked)
        ):
            raise ValueError("invalid_book")
        if any(a[0] > b[0] for a, b in zip(buy, buy[1:])) or any(
            a[0] < b[0] for a, b in zip(sell, sell[1:])
        ):
            raise ValueError("unsorted_book")
        self.last_us = at_us
        self.seen_quotes[quote_key] = fingerprint
        midpoint = (buy[0][0] + sell[0][0]) / 2
        self.ledger.apply(
            f"mark:{symbol}:{at_us}",
            at_us,
            "mark",
            symbol=symbol,
            price=mark if mark is not None else midpoint,
        )
        # Native stop remains active when app/model is unavailable. Once triggered,
        # a depth-limited remainder is a pending market exit even if price rebounds.
        position = self.ledger.positions.get(symbol)
        if position and (
            symbol in self.triggered_stops
            or (position.stop is not None and position.stop_active_us < at_us)
        ):
            trigger_price = self.ledger.marks[symbol]
            if (
                position.stop is not None
                and (trigger_price - position.stop) * (1 if position.quantity > 0 else -1) <= 0
            ):
                self.triggered_stops.add(symbol)
            if symbol in self.triggered_stops:
                levels = sell if position.quantity > 0 else buy
                for level in levels:
                    if symbol not in self.ledger.positions:
                        break
                    spec = self.ledger.instruments[symbol]
                    quantity = (
                        min(abs(self.ledger.positions[symbol].quantity), level[1])
                        / spec.quantity_step
                    ).to_integral_value(rounding=ROUND_FLOOR) * spec.quantity_step
                    if quantity:
                        self._close(
                            symbol,
                            level[0],
                            at_us,
                            f"stop_book_level_{levels.index(level)}",
                            quantity,
                        )
                        level[1] -= quantity
        self._management_quote(symbol, at_us, buy, sell)
        for order in sorted(self.orders.values(), key=lambda o: (o.submitted_us, o.order_id)):
            if order.symbol != symbol or order.state not in {"pending", "acknowledged", "partial"}:
                continue
            if order.cancel_us is not None and at_us >= order.cancel_us:
                self._finish(order, at_us, "cancelled")
                continue
            if at_us >= order.expires_us:
                self._finish(order, at_us, "expired")
                continue
            if (
                at_us < max(order.acknowledged_us, order.executable_us)
                or at_us <= order.submitted_us
            ):
                continue
            if order.state == "pending":
                order.state = "acknowledged"
                self.events.append(
                    {"kind": "acknowledgment", "order": order.order_id, "at_us": at_us}
                )
            levels = buy if order.signed_quantity > 0 else sell
            spec = self.ledger.instruments[symbol]
            for level in levels:
                price, depth = level
                if (
                    order.kind == "limit"
                    and (price - order.limit) * (1 if order.signed_quantity > 0 else -1) >= 0
                ):
                    continue
                quantity = (min(depth, order.remaining) / spec.quantity_step).to_integral_value(
                    rounding=ROUND_FLOOR
                ) * spec.quantity_step
                if not quantity:
                    continue
                if symbol not in self.ledger.positions and quantity * price < spec.minimum_notional:
                    continue
                order.fills += 1
                signed = quantity * (1 if order.signed_quantity > 0 else -1)
                fee = self.fees.charge(quantity * price, at_us, False)  # crossed limit is taker too
                try:
                    self.ledger.apply(
                        f"fill:{order.order_id}:{order.fills}",
                        at_us,
                        "fill",
                        symbol=symbol,
                        owner=order.owner,
                        quantity=signed,
                        price=price,
                        fee=fee,
                        leverage=order.leverage,
                        mode=order.mode,
                        reference_price=self.ledger.marks[symbol],
                        order_id=order.order_id,
                    )
                except ValueError as exc:
                    self.events.append(
                        {
                            "kind": "fill_rejected",
                            "order": order.order_id,
                            "reason": str(exc),
                            "at_us": at_us,
                        }
                    )
                    self._finish(order, at_us, "fill_rejected")
                    break
                level[1] -= quantity
                order.remaining -= quantity
                p = self.ledger.positions[symbol]
                if p.original_stop is None:
                    try:
                        self.ledger.apply(
                            f"protect:{order.order_id}",
                            at_us,
                            "stop",
                            symbol=symbol,
                            owner=order.owner,
                            price=order.stop,
                            original=True,
                        )
                    except ValueError:
                        # A gap can put the admitted stop on the wrong side of
                        # the fill. Exit against opposite-side depth, never at
                        # the entry price; retain any unfilled emergency exit.
                        self.triggered_stops.add(symbol)
                        self._finish(order, at_us, "cancelled_after_exit")
                        opposite = sell if p.quantity > 0 else buy
                        for exit_index, exit_level in enumerate(opposite):
                            if symbol not in self.ledger.positions:
                                break
                            exit_quantity = (
                                min(abs(self.ledger.positions[symbol].quantity), exit_level[1])
                                / spec.quantity_step
                            ).to_integral_value(rounding=ROUND_FLOOR) * spec.quantity_step
                            if exit_quantity:
                                self._close(
                                    symbol,
                                    exit_level[0],
                                    at_us,
                                    f"protection_failure_level_{exit_index}",
                                    exit_quantity,
                                )
                                exit_level[1] -= exit_quantity
                        break
                if order.target is not None:
                    self.targets[symbol] = order.target
                self.events.append(
                    {
                        "kind": "fill",
                        "order": order.order_id,
                        "at_us": at_us,
                        "quantity": str(signed),
                        "price": str(price),
                        "fee": str(fee),
                    }
                )
                if order.remaining == 0:
                    self._finish(order, at_us, "filled")
                    break
                order.state = "partial"

    def bar(
        self,
        symbol,
        start_us,
        end_us,
        *,
        open_price,
        high,
        low,
        close,
        spread_bps="0",
        extra_slippage_bps="0",
    ):
        """OHLC-only stop execution is an explicit adverse-path approximation.

        Only protection effective by interval start can act within this bar.
        Stops installed midway through a bar wait for subsequent observations.
        The same adverse spread/slippage overlay as minute-open fills applies
        to exits. Price precision is rounded against the closing side.
        """
        o, h, lo, c = map(lambda x: number(x, positive=True), (open_price, high, low, close))
        if not lo <= min(o, c) <= max(o, c) <= h or start_us > end_us or end_us < self.last_us:
            raise ValueError("invalid_bar")
        half = (
            number(spread_bps, nonnegative=True) / 20000
            + number(extra_slippage_bps, nonnegative=True) / 10000
        )
        if half >= 1:
            raise ValueError("invalid_execution_cost_fraction")
        self.last_us = end_us
        p = self.ledger.positions.get(symbol)
        if p and p.stop is not None and p.stop_active_us <= start_us:
            long = p.quantity > 0
            stop_hit = lo <= p.stop if long else h >= p.stop
            target = self.targets.get(symbol)
            target_hit = target is not None and (h >= target if long else lo <= target)
            if stop_hit and target_hit:
                self.events.append(
                    {"kind": "intrabar_ambiguity", "symbol": symbol, "at_us": end_us}
                )
            if stop_hit and (self.adverse_intrabar or not target_hit):
                price = min(o, p.stop) if long else max(o, p.stop)
                self._close(
                    symbol,
                    price * (1 - half if long else 1 + half),
                    end_us,
                    "stop_ohlc_adverse_assumption",
                    reference_price=price,
                )
            elif target_hit:
                self._close(
                    symbol,
                    target * (1 - half if long else 1 + half),
                    end_us,
                    "target_ohlc_assumption",
                    reference_price=target,
                )
        self.ledger.apply(f"bar-mark:{symbol}:{end_us}", end_us, "mark", symbol=symbol, price=c)

    def state(self):
        return {
            "ledger": self.ledger.state(),
            "orders": serial({k: asdict(v) for k, v in self.orders.items()}),
            "decisions": self.decisions,
            "events": self.events,
            "management": self.management,
        }

    def checkpoint(self):
        payload = {
            "schema": "liquid-simulation-checkpoint-v1",
            "initial_cash": str(self.ledger.initial_cash),
            "instruments": serial({s: asdict(i) for s, i in self.ledger.instruments.items()}),
            "fees": serial(asdict(self.fees)),
            "ledger_journal": self.ledger.journal,
            "orders": serial({k: asdict(v) for k, v in self.orders.items()}),
            "decisions": self.decisions,
            "events": self.events,
            "targets": serial(self.targets),
            "seen_signals": self.seen_signals,
            "seen_quotes": self.seen_quotes,
            "triggered_stops": sorted(self.triggered_stops),
            "halted": self.halted,
            "last_us": self.last_us,
            "adverse_intrabar": self.adverse_intrabar,
            "management": self.management,
            "order_sequence": list(self.orders),
            "management_sequence": list(self.management),
        }
        return {"payload": deepcopy(payload), "sha256": digest(payload)}

    def save(self, path: Path):
        temporary = path.with_suffix(path.suffix + ".pending")
        with temporary.open("w") as stream:
            stream.write(json.dumps(self.checkpoint(), sort_keys=True, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    @classmethod
    def restore(cls, checkpoint):
        value = deepcopy(checkpoint["payload"])
        if checkpoint["sha256"] != digest(value):
            raise ValueError("checkpoint_checksum_mismatch")
        if value["schema"] != "liquid-simulation-checkpoint-v1":
            raise ValueError("checkpoint_schema_unsupported")
        instruments = {}
        for symbol, data in value["instruments"].items():
            data = dict(data)
            for key in ("quantity_step", "price_step", "minimum_notional"):
                data[key] = D(data[key])
            data["tiers"] = tuple(
                Tier(D(t["lower"]), D(t["maximum_leverage"])) for t in data["tiers"]
            )
            instruments[symbol] = Instrument(**data)
        ledger = Ledger.replay(D(value["initial_cash"]), instruments, value["ledger_journal"])
        fee_data = dict(value["fees"])
        for key in ("maker", "taker"):
            fee_data[key] = D(fee_data[key])
        sim = cls(ledger, FeeSchedule(**fee_data), adverse_intrabar=value["adverse_intrabar"])
        for key in value["order_sequence"]:
            raw = value["orders"][key]
            data = dict(raw)
            for field in ("signed_quantity", "remaining", "stop", "target", "leverage", "limit"):
                if data[field] is not None:
                    data[field] = D(data[field])
            sim.orders[key] = Order(**data)
        for key in (
            "decisions",
            "events",
            "seen_signals",
            "seen_quotes",
            "halted",
            "last_us",
            "management",
        ):
            setattr(sim, key, value[key])
        sim.targets = {s: D(v) for s, v in value["targets"].items()}
        sim.triggered_stops = set(value["triggered_stops"])
        sim.management = {key: value["management"][key] for key in value["management_sequence"]}
        return sim
