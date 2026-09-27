"""Conservative normalization for the observed Liquid Computer surface.

This module has no network client and cannot submit an order.  It preserves the
difference between locally receiving a payload and a broker-authoritative event.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from liquid_autonomous_trader.btc_mandate import (
    BTC_MAX_ORDER_NOTIONAL_USD,
    OTHER_STRATEGY_MAX_ORDER_NOTIONAL_USD,
)
from liquid_autonomous_trader.flow_mandate import FLOW_MAX_ORDER_NOTIONAL_USD
from liquid_autonomous_trader.xyz_mandate import XYZ_MAX_ORDER_NOTIONAL_USD

MAX_AUTOMATED_ORDER_NOTIONAL_USD = BTC_MAX_ORDER_NOTIONAL_USD


class ComputerContractError(ValueError):
    """The payload does not match the narrow, observed Computer representation."""


class ComputerRoute(StrEnum):
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class ComputerPosition:
    broker_symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    margin_used_usd: Decimal
    stop_price: Decimal | None
    unrealized_pnl_usd: Decimal
    reported_symbol: str | None = None


@dataclass(frozen=True)
class ComputerPortfolio:
    """A locally received snapshot; it contains no provider observation timestamp."""

    route: ComputerRoute
    equity_usd: Decimal
    available_collateral_usd: Decimal
    margin_used_usd: Decimal
    positions: tuple[ComputerPosition, ...]
    received_at: datetime
    reported_username: str | None = None
    account_id: None = None
    provider_observed_at: None = None


@dataclass(frozen=True)
class ComputerOpenOrder:
    order_id: str
    symbol: str
    side: str
    kind: str
    quantity: Decimal
    trigger_or_limit_price: Decimal
    flags: tuple[str, ...]
    position_level: bool
    reduce_only: bool
    venue_verified_fixed_stop: bool = False


@dataclass(frozen=True)
class ComputerOpenOrders:
    orders: tuple[ComputerOpenOrder, ...]
    received_at: datetime
    provider_observed_at: None = None
    history_complete: bool = False
    working_snapshot_complete: bool = True


@dataclass(frozen=True)
class ComputerReadiness:
    market_order_mapping_supported: bool
    portfolio_normalization_supported: bool
    open_order_normalization_supported: bool
    execution_ready: bool
    blockers: tuple[str, ...]
    contained_limitations: tuple[str, ...]


_ORDER_LINE = re.compile(
    r"^- id=(?P<id>[^ |]+) \| (?P<symbol>[A-Za-z0-9.:-]+) "
    r"(?P<side>BUY|SELL) (?P<kind>limit|trigger|tp|sl) \| size "
    r"(?P<size>[0-9]+(?:\.[0-9]+)?) \| (?:limit|trigger) "
    r"\$(?P<price>(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?)"
    r"(?P<flags>(?: \| [a-z-]+)*)"
    r"(?: \| (?:tif=(?P<tif>Gtc|Ioc|Alo) \| )?placed (?P<placed>[^\s|]+))?$"
)
_ORDER_FOOTER = (
    "kind: limit = resting order; tp/sl = take-profit/stop-loss; trigger = other conditional. "
    "position-level flag means the trigger applies to the whole position. "
    "Pass id to cancel_order to cancel."
)
_EMPTY_ORDERS = frozenset(
    {
        "no open orders.",
        "no open orders. no resting limits.",
        "no open orders. no resting limits and no position-level tp/sl triggers are set.",
    }
)
_TRUSTED_ORDER_FLAGS = frozenset({"position-level", "reduce-only"})


def normalize_portfolio(
    payload: Mapping[str, object], *, received_at: datetime
) -> ComputerPortfolio:
    """Normalize the structured shape observed in the user's existing integration."""

    _aware(received_at)
    if "status" in payload and payload["status"] != {
        "account": "ok",
        "positions": "ok",
        "tpsl": "ok",
    }:
        raise ComputerContractError("portfolio_snapshot_incomplete")
    paper = payload.get("paper")
    account = payload.get("account")
    positions = payload.get("positions")
    if not isinstance(paper, bool) or not isinstance(account, Mapping):
        raise ComputerContractError("portfolio_shape_incomplete")
    if not isinstance(positions, Sequence) or isinstance(positions, (str, bytes)):
        raise ComputerContractError("portfolio_positions_invalid")
    parsed = tuple(_position(item) for item in positions)
    if len({p.broker_symbol for p in parsed}) != len(parsed):
        raise ComputerContractError("portfolio_duplicate_net_position")
    username = account.get("username")
    if username is not None and (
        not isinstance(username, str) or not username or username != username.strip()
    ):
        raise ComputerContractError("portfolio_username_invalid")
    return ComputerPortfolio(
        route=ComputerRoute.PAPER if paper else ComputerRoute.LIVE,
        equity_usd=_decimal(account, "equity", positive=False),
        available_collateral_usd=_decimal(account, "available_balance", positive=False),
        margin_used_usd=_decimal(account, "margin_used", positive=False),
        positions=parsed,
        received_at=received_at,
        reported_username=username,
    )


def require_account_binding(
    portfolio: ComputerPortfolio,
    *,
    expected_username: str,
    expected_route: ComputerRoute,
    now: datetime,
) -> None:
    """Check fresh authenticated readback; never infer position ownership.

    Callers must obtain the portfolio through their authenticated credential, not
    caller-controlled strategy JSON. A username is not an immutable broker ID.
    This guard alone does not authorize an order or establish execution readiness.
    """
    _aware(now)
    _aware(portfolio.received_at)
    if (
        not isinstance(expected_username, str)
        or not expected_username
        or expected_username != expected_username.strip()
        or not isinstance(expected_route, ComputerRoute)
    ):
        raise ComputerContractError("account_binding_configuration_invalid")
    if portfolio.reported_username != expected_username:
        raise ComputerContractError("account_binding_username_mismatch")
    if portfolio.route is not expected_route:
        raise ComputerContractError("account_binding_route_mismatch")
    age = (now - portfolio.received_at).total_seconds()
    if not 0 <= age <= 5:
        raise ComputerContractError("account_binding_snapshot_expired")


def normalize_open_orders(
    payload: Mapping[str, object], *, received_at: datetime
) -> ComputerOpenOrders:
    """Parse only the exact order lines already observed; reject prose drift."""

    _aware(received_at)
    if "orders" in payload:
        return _structured_open_orders(payload, received_at=received_at)
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ComputerContractError("open_orders_text_missing")
    stripped = text.strip()
    if stripped.lower() in _EMPTY_ORDERS:
        return ComputerOpenOrders(orders=(), received_at=received_at)
    lines = stripped.splitlines()
    if len(lines) < 2 or not re.fullmatch(r"[1-9][0-9]* open orders?:?", lines[0].strip()):
        raise ComputerContractError("open_orders_header_unrecognized")
    rows = [line.strip() for line in lines[1:] if line.strip()]
    if rows and rows[-1] == _ORDER_FOOTER:
        rows.pop()
    orders = tuple(_order(line) for line in rows)
    if int(lines[0].split()[0]) != len(orders):
        raise ComputerContractError("open_orders_count_mismatch")
    order_ids = tuple(order.order_id for order in orders)
    if len(set(order_ids)) != len(order_ids):
        raise ComputerContractError("open_orders_duplicate_id")
    return ComputerOpenOrders(orders=orders, received_at=received_at)


def _structured_open_orders(payload, *, received_at):
    """September 25 typed listing; partial scopes never imply an empty account."""
    if (
        type(payload.get("paper")) is not bool
        or not isinstance(payload.get("wallet"), str)
        or not re.fullmatch(r"0x[0-9a-fA-F]{40}", payload["wallet"])
        or payload.get("status") != {"orders": "ok", "failedScopes": []}
        or not isinstance(payload.get("orders"), list)
    ):
        raise ComputerContractError("open_orders_snapshot_incomplete")
    orders = tuple(_structured_order(row) for row in payload["orders"])
    if len({o.order_id for o in orders}) != len(orders):
        raise ComputerContractError("open_orders_duplicate_id")
    return ComputerOpenOrders(orders=orders, received_at=received_at)


def _structured_order(row):
    fields = {
        "id",
        "symbol",
        "side",
        "kind",
        "size",
        "limitPx",
        "triggerPx",
        "reduceOnly",
        "positionLevel",
        "tif",
        "placedAt",
    }
    if not isinstance(row, Mapping) or set(row) != fields:
        raise ComputerContractError("open_order_shape_invalid")
    if (
        type(row["id"]) is not int
        or row["id"] < 0
        or not isinstance(row["symbol"], str)
        or not re.fullmatch(r"[A-Za-z0-9.:-]+", row["symbol"])
        or row["side"] not in ("buy", "sell")
        or row["kind"] not in ("limit", "trigger", "tp", "sl")
        or type(row["reduceOnly"]) is not bool
        or type(row["positionLevel"]) is not bool
        or row["tif"] not in (None, "Gtc", "Ioc", "Alo")
    ):
        raise ComputerContractError("open_order_identity_invalid")
    try:
        _aware(datetime.fromisoformat(row["placedAt"]))
    except (ValueError, TypeError):
        raise ComputerContractError("open_order_placement_time_invalid") from None
    quantity = _number(row["size"], "order_size", positive=False)
    if quantity == 0 and not (
        row["kind"] in ("sl", "tp", "trigger") and row["reduceOnly"] and row["positionLevel"]
    ):
        raise ComputerContractError("zero_size_requires_whole_position_trigger")
    price = row["limitPx"] if row["kind"] == "limit" else row["triggerPx"]
    flags = tuple(
        flag
        for flag, present in (
            ("position-level", row["positionLevel"]),
            ("reduce-only", row["reduceOnly"]),
        )
        if present
    )
    return ComputerOpenOrder(
        order_id=str(row["id"]),
        symbol=canonical_symbol(row["symbol"]),
        side=row["side"],
        kind=row["kind"],
        quantity=quantity,
        trigger_or_limit_price=_number(price, "order_price"),
        flags=flags,
        position_level=row["positionLevel"],
        reduce_only=row["reduceOnly"],
    )


def build_market_order_arguments(
    *,
    symbol: str,
    side: str,
    notional_usd: Decimal,
    leverage: Decimal,
    stop: Decimal,
    strategy: str | None = None,
) -> dict[str, object]:
    """Build the currently documented Computer input without sending it."""

    if not symbol or side not in {"long", "short"}:
        raise ComputerContractError("market_order_identity_invalid")
    for name, value in (("notional", notional_usd), ("leverage", leverage), ("stop", stop)):
        if not value.is_finite() or value <= 0:
            raise ComputerContractError(f"market_order_{name}_invalid")
    if leverage != leverage.to_integral_value():
        raise ComputerContractError("market_order_leverage_not_integer")
    from liquid_autonomous_trader.frozen.flow_mirror import FLOW_MIRROR_V1_ALLOWED_SYMBOLS

    maximum = (
        MAX_AUTOMATED_ORDER_NOTIONAL_USD
        if symbol == "BTC"
        else XYZ_MAX_ORDER_NOTIONAL_USD
        if symbol == "xyz:XYZ100"
        else FLOW_MAX_ORDER_NOTIONAL_USD
        if symbol in {"xyz:" + ticker for ticker in FLOW_MIRROR_V1_ALLOWED_SYMBOLS}
        else OTHER_STRATEGY_MAX_ORDER_NOTIONAL_USD
    )
    if strategy == "inverse_cramer":
        maximum = Decimal(500)
        if leverage != Decimal(10):
            raise ComputerContractError("cramer_ten_x_required")
    if notional_usd > maximum:
        raise ComputerContractError("market_order_notional_limit_exceeded")
    return {
        "symbol": symbol,
        "side": "buy" if side == "long" else "sell",
        "size": _exact_float(notional_usd, "notional"),
        "leverage": int(leverage),
        "type": "market",
        "sl": _exact_float(stop, "stop"),
    }


def computer_readiness() -> ComputerReadiness:
    """Provider reply resolves semantics, not installed end-to-end release checks."""

    return ComputerReadiness(
        market_order_mapping_supported=True,
        portfolio_normalization_supported=True,
        open_order_normalization_supported=True,
        execution_ready=False,
        blockers=(
            "exclusive_account_runtime_reconciliation_pending",
            "initial_stop_failure_path_unverified",
            "stop_replacement_transition_unverified",
            "installed_execution_lifecycle_unverified",
            "production_risk_inputs_and_source_dispatch_pending",
            "execution_alert_delivery_join_pending",
        ),
        contained_limitations=(
            "caller_stable_idempotency_unsupported",
            "query_by_intent_unsupported",
            "authoritative_fills_unsupported",
            "net_position_not_per_order_fills",
            "delayed_writes_not_position_bound",
            "local_receipt_time_not_provider_time",
        ),
    )


_CRAMER_ALIASES: dict[str, str] = {}


def register_cramer_aliases(coin):
    from liquid_autonomous_trader.cramer_market import valid_coin

    if not valid_coin(coin):
        raise ComputerContractError("cramer_alias_identity_invalid")
    # Namespaced position aliases only. Bare symbols can collide across dexes;
    # those require independent venue/order evidence, never a global rename.
    alias = coin + "-PERP"
    if alias in _CRAMER_ALIASES and _CRAMER_ALIASES[alias] != coin:
        raise ComputerContractError("cramer_alias_collision")
    _CRAMER_ALIASES[alias] = coin


def canonical_symbol(symbol: str) -> str:
    # Only the installed native market inventory has aliases. Other dexes and
    # arbitrary suffixes retain their identity, never collapse into an owned market.
    from liquid_autonomous_trader.frozen.flow_mirror import FLOW_MIRROR_V1_ALLOWED_SYMBOLS

    aliases = {
        "BTC-PERP": "BTC",
        "xyz:XYZ100-PERP": "xyz:XYZ100",
        # Liquid open-order label verified against the identical venue order ID.
        "NASDAQ100": "xyz:XYZ100",
    }
    for ticker in FLOW_MIRROR_V1_ALLOWED_SYMBOLS | {"XYZ100"}:
        aliases[ticker] = "xyz:" + ticker
        aliases["xyz:" + ticker + "-PERP"] = "xyz:" + ticker
    return aliases.get(symbol, _CRAMER_ALIASES.get(symbol, symbol))


def _position(value: object) -> ComputerPosition:
    if not isinstance(value, Mapping):
        raise ComputerContractError("portfolio_position_invalid")
    symbol = value.get("symbol")
    side = value.get("side")
    if not isinstance(symbol, str) or not symbol or side not in {"long", "short"}:
        raise ComputerContractError("portfolio_position_identity_invalid")
    stop = value.get("sl")
    return ComputerPosition(
        # Explicit observed alias only. Never strip arbitrary suffixes or trust
        # displayName to rename an unrelated market.
        broker_symbol=canonical_symbol(symbol),
        side=side,
        quantity=_decimal(value, "size"),
        entry_price=_decimal(value, "entryPx"),
        mark_price=_decimal(value, "markPx"),
        leverage=_decimal(value, "leverage"),
        margin_used_usd=_decimal(value, "marginUsed", positive=False),
        stop_price=None if stop is None else _number(stop, "sl"),
        unrealized_pnl_usd=_decimal(value, "unrealizedPnl", positive=None, default="0"),
        reported_symbol=symbol,
    )


def _order(line: str) -> ComputerOpenOrder:
    match = _ORDER_LINE.fullmatch(line)
    if match is None:
        raise ComputerContractError("open_order_line_unrecognized")
    flags = tuple(part.strip() for part in match.group("flags").split("|") if part.strip())
    if match.group("placed"):
        try:
            _aware(datetime.fromisoformat(match.group("placed")))
        except ValueError:
            raise ComputerContractError("open_order_placement_time_invalid") from None
    if any(flag not in _TRUSTED_ORDER_FLAGS for flag in flags):
        raise ComputerContractError("open_order_flag_unrecognized")
    quantity = _number(match.group("size"), "order_size", positive=False)
    if quantity == 0 and not (
        match.group("kind") in {"sl", "tp", "trigger"}
        and {"position-level", "reduce-only"}.issubset(flags)
    ):
        raise ComputerContractError("zero_size_requires_whole_position_trigger")
    return ComputerOpenOrder(
        order_id=match.group("id"),
        symbol=canonical_symbol(match.group("symbol")),
        side=match.group("side").lower(),
        kind=match.group("kind"),
        quantity=quantity,
        trigger_or_limit_price=_number(match.group("price").replace(",", ""), "order_price"),
        flags=flags,
        position_level="position-level" in flags,
        reduce_only="reduce-only" in flags,
    )


def _decimal(
    value: Mapping[str, object],
    key: str,
    *,
    positive: bool | None = True,
    default: object | None = None,
) -> Decimal:
    raw = value.get(key, default)
    if raw is None:
        raise ComputerContractError(f"{key}_missing")
    return _number(raw, key, positive=positive)


def _number(value: object, name: str, *, positive: bool | None = True) -> Decimal:
    if isinstance(value, bool):
        raise ComputerContractError(f"{name}_invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ComputerContractError(f"{name}_invalid") from exc
    if not result.is_finite():
        raise ComputerContractError(f"{name}_invalid")
    invalid_sign = (positive is True and result <= 0) or (positive is False and result < 0)
    if invalid_sign:
        raise ComputerContractError(f"{name}_invalid")
    return result


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ComputerContractError("local_received_at_must_be_aware")


def _exact_float(value: Decimal, name: str) -> float:
    converted = float(value)
    if Decimal(str(converted)) != value:
        raise ComputerContractError(f"market_order_{name}_not_exactly_representable")
    return converted
