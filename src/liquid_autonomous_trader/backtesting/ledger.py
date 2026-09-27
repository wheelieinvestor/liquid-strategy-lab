"""Deterministic perpetual-account economics; no broker or network capability.

Margin equations follow native tier deductions and signed oracle funding. Exact
liquidation *execution* still needs book/mark history; a maintenance breach alone
is not proof of a fill or survival. All monetary inputs are Decimal strings.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Context, Decimal, localcontext
from functools import wraps

from liquid_autonomous_trader.backtesting.events import digest

D = Decimal
ZERO = D(0)
CONTEXT = Context(prec=28)  # production Decimal arithmetic
HOUR_US = 3_600_000_000


def exact(function):
    @wraps(function)
    def call(*args, **kwargs):
        with localcontext(CONTEXT):
            return function(*args, **kwargs)

    return call


def number(value, *, positive=False, nonnegative=False):
    if isinstance(value, (float, bool)):
        raise ValueError("decimal_string_required")
    value = D(value)
    if not value.is_finite() or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError("invalid_decimal")
    return value


def serial(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serial(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serial(v) for v in value]
    return value


@dataclass(frozen=True)
class Tier:
    lower: Decimal
    maximum_leverage: Decimal


@dataclass(frozen=True)
class Instrument:
    symbol: str
    quantity_step: Decimal
    price_step: Decimal
    minimum_notional: Decimal
    tiers: tuple[Tier, ...]
    metadata_hash: str
    quality: str  # observed, partial, synthetic_assumption

    def __post_init__(self):
        number(self.quantity_step, positive=True)
        number(self.price_step, positive=True)
        number(self.minimum_notional, nonnegative=True)
        if not self.tiers or self.tiers[0].lower != 0:
            raise ValueError("maintenance_tiers_must_start_at_zero")
        for i, tier in enumerate(self.tiers):
            number(tier.lower, nonnegative=True)
            number(tier.maximum_leverage, positive=True)
            if i and (
                tier.lower <= self.tiers[i - 1].lower
                or tier.maximum_leverage > self.tiers[i - 1].maximum_leverage
            ):
                raise ValueError("invalid_maintenance_tiers")
        if not self.metadata_hash or self.quality not in {
            "observed",
            "partial",
            "synthetic_assumption",
        }:
            raise ValueError("metadata_provenance_required")

    @exact
    def maintenance(self, notional: Decimal) -> Decimal:
        number(notional, nonnegative=True)
        deduction, previous_rate = ZERO, ZERO
        selected = self.tiers[0]
        for tier in self.tiers:
            if notional < tier.lower:
                break
            rate = 1 / (2 * tier.maximum_leverage)
            deduction += tier.lower * (rate - previous_rate)
            previous_rate, selected = rate, tier
        return notional / (2 * selected.maximum_leverage) - deduction


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    route: str
    tier: str
    referral: str
    start_us: int
    end_us: int
    taker: Decimal
    maker: Decimal
    quality: str

    @exact
    def charge(self, notional: Decimal, at_us: int, maker: bool) -> Decimal:
        number(notional, nonnegative=True)
        if not self.start_us <= at_us < self.end_us:
            raise ValueError("fee_schedule_outside_effective_window")
        rate = number(self.maker if maker else self.taker)
        return notional * rate


def native_total_fee(fill: dict) -> Decimal:
    """Native `fee` includes builderFee; rebates retain their negative sign."""
    if fill.get("feeToken") != "USDC":
        raise ValueError("unsupported_fee_currency")
    return number(fill["fee"])


@dataclass
class Position:
    symbol: str
    owner: str
    quantity: Decimal
    entry: Decimal
    leverage: Decimal
    mode: str
    isolated_cash: Decimal = ZERO
    stop: Decimal | None = None
    original_stop: Decimal | None = None
    stop_active_us: int | None = None


class Ledger:
    def __init__(self, initial_cash: Decimal, instruments: dict[str, Instrument]):
        self.initial_cash = number(initial_cash, positive=True)
        self.cross_cash = self.initial_cash
        self.realized = ZERO
        self.fees = ZERO
        self.funding = ZERO
        self.turnover = ZERO
        self.slippage = ZERO  # informational decomposition, already in fill price
        self.instruments = instruments
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, Decimal] = {}
        self.reservations: dict[str, dict] = {}
        self.journal: list[dict] = []
        self.applied: dict[str, str] = {}
        self.last_us = 0
        self.funding_settlements: dict[tuple, str] = {}

    @exact
    def apply(self, event_id: str, at_us: int, kind: str, **data) -> bool:
        """Atomic, idempotent reducer; conflicting retries never mutate state."""
        body = {"id": event_id, "at_us": at_us, "kind": kind, "data": serial(data)}
        fingerprint = digest(body)
        if event_id in self.applied:
            if self.applied[event_id] != fingerprint:
                raise ValueError("conflicting_ledger_retry")
            return False
        if not event_id or at_us < self.last_us:
            raise ValueError("ledger_clock_regressed")
        before = {
            k: deepcopy(v)
            for k, v in self.__dict__.items()
            if k not in {"journal", "applied", "instruments"}
        }
        try:
            handler = {
                "fill": self._fill,
                "mark": self._mark,
                "funding": self._funding,
                "reserve": self._reserve,
                "release": self._release,
                "resolve": self._resolve,
                "stop": self._stop,
            }.get(kind)
            if handler is None:
                raise ValueError("unsupported_ledger_event")
            handler(at_us, **data)
            self.last_us = at_us
            self.reconcile()
            self.applied[event_id] = fingerprint
            self.journal.append(body)
        except Exception:
            self.__dict__.update(before)
            raise
        return True

    def _mark(self, at_us, *, symbol, price):
        if symbol not in self.instruments:
            raise ValueError("unknown_instrument")
        self.marks[symbol] = number(price, positive=True)

    def _reserve(self, at_us, *, order_id, symbol, owner, collateral, state="pending"):
        if state not in {"pending", "unknown"}:
            raise ValueError("invalid_reservation_state")
        amount = number(collateral, nonnegative=True)
        if order_id in self.reservations:
            raise ValueError("reservation_exists")
        if symbol in self.positions and self.positions[symbol].owner != owner:
            raise ValueError("foreign_position_ownership")
        if any(r["symbol"] == symbol for r in self.reservations.values()):
            raise ValueError("market_already_reserved")
        if amount > self.available():
            raise ValueError("insufficient_available_collateral")
        self.reservations[order_id] = dict(
            symbol=symbol, owner=owner, collateral=amount, state=state
        )

    def _resolve(self, at_us, *, order_id):
        self.reservations[order_id]["state"] = "pending"

    def _release(self, at_us, *, order_id, outcome_known):
        if not outcome_known:
            raise ValueError("unknown_obligation_cannot_be_released")
        self.reservations.pop(order_id)

    def _fill(
        self,
        at_us,
        *,
        symbol,
        owner,
        quantity,
        price,
        fee,
        leverage,
        mode,
        reduce_only=False,
        reference_price=None,
        order_id=None,
    ):
        spec = self.instruments[symbol]
        quantity, price, fee = number(quantity), number(price, positive=True), number(fee)
        leverage = number(leverage, positive=True)
        if not quantity or abs(quantity) % spec.quantity_step or price % spec.price_step:
            raise ValueError("invalid_native_precision")
        if mode not in {"cross", "isolated"}:
            raise ValueError("margin_mode_required")
        existing = self.positions.get(symbol)
        if existing and (
            existing.owner != owner or existing.mode != mode or existing.leverage != leverage
        ):
            raise ValueError("position_ownership_or_margin_mismatch")
        old = existing.quantity if existing else ZERO
        reducing = old * quantity < 0
        if reduce_only and (not reducing or abs(quantity) > abs(old)):
            raise ValueError("invalid_reduce_only")
        if reducing and abs(quantity) > abs(old):
            raise ValueError("position_flip_requires_separate_entry")
        if not reducing and order_id is None and abs(quantity * price) < spec.minimum_notional:
            raise ValueError("minimum_notional")
        resulting_notional = abs((old + quantity) * price)
        tier = max((t for t in spec.tiers if t.lower <= resulting_notional), key=lambda t: t.lower)
        if not reducing and leverage > tier.maximum_leverage:
            raise ValueError("tier_leverage_exceeded")
        if order_id is not None:
            reserve = self.reservations[order_id]
            if reserve["symbol"] != symbol or reserve["owner"] != owner:
                raise ValueError("reservation_identity_mismatch")
        self.marks.setdefault(symbol, price)
        if existing is None:
            existing = Position(symbol, owner, ZERO, price, leverage, mode)
            self.positions[symbol] = existing
        pnl = ZERO
        if reducing:
            pnl = abs(quantity) * (price - existing.entry) * (1 if old > 0 else -1)
        else:
            additional = abs(quantity * price) / leverage
            released = (
                min(additional, self.reservations[order_id]["collateral"]) if order_id else ZERO
            )
            if additional + max(fee, ZERO) > self.available() + released:
                raise ValueError("fill_exceeds_available_collateral")
            existing.entry = (abs(old) * existing.entry + abs(quantity) * price) / abs(
                old + quantity
            )
            if mode == "isolated":
                self.cross_cash -= additional
                existing.isolated_cash += additional
            if order_id:
                self.reservations[order_id]["collateral"] -= released
        existing.quantity += quantity
        if mode == "isolated":
            existing.isolated_cash += pnl - fee
        else:
            self.cross_cash += pnl - fee
        self.realized += pnl
        self.fees += fee
        self.turnover += abs(quantity * price)
        if reference_price is not None:
            self.slippage += quantity * (price - number(reference_price, positive=True))
        self.marks.setdefault(symbol, price)
        if existing.quantity == 0:
            self.cross_cash += existing.isolated_cash
            del self.positions[symbol]

    def _funding(self, at_us, *, symbol, oracle_price, rate, settlement_us, native_timestamp=False):
        # Caller must order fills and settlement explicitly at identical timestamps.
        if settlement_us != at_us or (at_us % HOUR_US and not native_timestamp):
            raise ValueError("funding_settlement_boundary_required")
        oracle, rate = number(oracle_price, positive=True), number(rate)
        key = (symbol, settlement_us // HOUR_US)
        fingerprint = digest([str(oracle), str(rate)])
        if key in self.funding_settlements:
            if self.funding_settlements[key] != fingerprint:
                raise ValueError("conflicting_funding_settlement")
            return
        self.funding_settlements[key] = fingerprint
        position = self.positions.get(symbol)
        payment = -position.quantity * oracle * rate if position else ZERO
        if position and position.mode == "isolated":
            position.isolated_cash += payment
        else:
            self.cross_cash += payment
        self.funding += payment

    def _stop(self, at_us, *, symbol, owner, price, original=False, allow_loosen=False):
        p = self.positions[symbol]
        if p.owner != owner:
            raise ValueError("stop_ownership_mismatch")
        price = number(price, positive=True)
        if price % self.instruments[symbol].price_step:
            raise ValueError("invalid_stop_precision")
        direction = 1 if p.quantity > 0 else -1
        mark = self.marks[symbol]
        if direction * (mark - price) <= 0:
            raise ValueError("crossed_stop")
        if original:
            if p.original_stop is not None:
                raise ValueError("original_stop_immutable")
            p.original_stop = price
        elif p.original_stop is None or direction * (price - p.original_stop) < 0:
            raise ValueError("stop_exceeds_original_risk")
        elif p.stop is not None and direction * (price - p.stop) < 0 and not allow_loosen:
            raise ValueError("stop_loosening_not_approved")
        p.stop, p.stop_active_us = price, at_us

    @exact
    def unrealized(self, p: Position) -> Decimal:
        return p.quantity * (self.marks[p.symbol] - p.entry)

    @exact
    def cash(self) -> Decimal:
        return self.cross_cash + sum((p.isolated_cash for p in self.positions.values()), ZERO)

    @exact
    def equity(self) -> Decimal:
        return self.cash() + sum((self.unrealized(p) for p in self.positions.values()), ZERO)

    @exact
    def margin(self, p: Position) -> Decimal:
        return abs(p.quantity * self.marks[p.symbol]) / p.leverage

    @exact
    def available(self) -> Decimal:
        cross = [p for p in self.positions.values() if p.mode == "cross"]
        return (
            self.cross_cash
            + sum((self.unrealized(p) - self.margin(p) for p in cross), ZERO)
            - sum((r["collateral"] for r in self.reservations.values()), ZERO)
        )

    @exact
    def breaches(self) -> list[dict]:
        cross = [p for p in self.positions.values() if p.mode == "cross"]
        cross_equity = self.cross_cash + sum((self.unrealized(p) for p in cross), ZERO)
        cross_mm = sum(
            (
                self.instruments[p.symbol].maintenance(abs(p.quantity * self.marks[p.symbol]))
                for p in cross
            ),
            ZERO,
        )
        result = []
        if cross and cross_equity < cross_mm:
            result.append(
                {
                    "scope": "cross",
                    "equity": str(cross_equity),
                    "maintenance": str(cross_mm),
                    "backstop_threshold": cross_equity < 2 * cross_mm / 3,
                }
            )
        for p in self.positions.values():
            mm = self.instruments[p.symbol].maintenance(abs(p.quantity * self.marks[p.symbol]))
            equity = p.isolated_cash + self.unrealized(p)
            if p.mode == "isolated" and equity < mm:
                result.append(
                    {
                        "scope": p.symbol,
                        "equity": str(equity),
                        "maintenance": str(mm),
                        "backstop_threshold": equity < 2 * mm / 3,
                    }
                )
        return result

    @exact
    def reconcile(self):
        expected = self.initial_cash + self.realized - self.fees + self.funding
        # Decimal rounding can accumulate below the native currency precision.
        if abs(self.cash() - expected) > D("1e-18"):
            raise ArithmeticError("cash_ledger_does_not_reconcile")
        if any(p.quantity == 0 for p in self.positions.values()):
            raise ArithmeticError("flat_position_retained")
        return True

    def state(self) -> dict:
        return serial(
            {
                "initial_cash": self.initial_cash,
                "cash": self.cash(),
                "equity": self.equity(),
                "realized": self.realized,
                "fees": self.fees,
                "funding": self.funding,
                "turnover": self.turnover,
                "slippage": self.slippage,
                "available": self.available(),
                "positions": {s: asdict(p) for s, p in sorted(self.positions.items())},
                "marks": self.marks,
                "reservations": self.reservations,
                "breaches": self.breaches(),
            }
        )

    @classmethod
    def replay(cls, initial_cash, instruments, journal):
        ledger = cls(initial_cash, instruments)
        for row in journal:
            ledger.apply(row["id"], row["at_us"], row["kind"], **row["data"])
        return ledger
