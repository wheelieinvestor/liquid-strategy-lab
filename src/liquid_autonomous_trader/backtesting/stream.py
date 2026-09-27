"""Bounded public book/trade stream; no user subscriptions or write messages."""

from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path

from liquid_autonomous_trader.backtesting.events import Catalog, Event, canonical

WS_URL = "wss://api.hyperliquid.xyz/ws"
SOURCE = "hyperliquid-public-websocket-v1"


def observation(store, kind, coin, stamp, received, payload, sequence=0, quality="observed"):
    prior = store.latest(SOURCE, kind, coin, stamp, sequence=sequence)
    if prior and prior.payload == payload:
        return 0
    event = Event(
        source=SOURCE,
        instrument=coin,
        venue="hyperliquid",
        kind=kind,
        event_us=stamp,
        published_us=stamp,
        available_us=received,
        retrieved_us=received,
        sequence=sequence,
        revision=0,
        units="USD/base",
        quality=quality,
        reference="hyperliquid-public-websocket",
        payload_json=canonical(payload),
    )
    if prior:
        event = replace(event, revision=prior.revision + 1, lineage=(prior.content_hash,))
    return store.append([event], cursor=("stream:" + coin, str(received)))


def ingest_message(store, message, *, coins, received_us):
    channel, data = message.get("channel"), message.get("data")
    if channel in {"subscriptionResponse", "pong"}:
        return 0
    if channel not in {"trades", "l2Book"}:
        raise ValueError("unsubscribed_stream_channel")
    rows = data if channel == "trades" else [data]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("stream_batch_budget")
    count = 0
    for row in rows:
        coin = row["coin"]
        if coin not in coins:
            raise ValueError("unsubscribed_instrument")
        if channel == "trades":
            # Public trade messages contain buyer/seller addresses. Deliberately
            # retain only market economics and the documented time/coin/tid key.
            payload = {key: row[key] for key in ("coin", "side", "px", "sz", "time", "tid")}
            sequence = int(row["tid"])
        else:
            payload = {key: row[key] for key in ("coin", "time", "levels")}
            sequence = 0
        count += observation(
            store,
            "trade" if channel == "trades" else "native_book",
            coin,
            int(row["time"]) * 1000,
            received_us,
            payload,
            sequence,
        )
    return count


def capture_stream(path: Path, *, coins=("BTC",), seconds=10, max_messages=1000, connect=None):
    if not 1 <= seconds <= 60 or not 1 <= max_messages <= 10000 or not 1 <= len(coins) <= 8:
        raise ValueError("stream_capture_budget")
    if len(set(coins)) != len(coins) or any(
        not re.fullmatch(r"[A-Za-z0-9:.]{1,30}", c) for c in coins
    ):
        raise ValueError("invalid_public_instrument")
    from websockets.exceptions import WebSocketException

    if connect is None:
        from websockets.sync.client import connect
    store = Catalog(path)
    deadline = time.monotonic() + seconds
    failures, messages, inserted = [], 0, 0

    def gap(reason):
        stamp = time.time_ns() // 1000
        for coin in coins:
            observation(
                store,
                "gap",
                coin,
                stamp,
                stamp,
                {"reason": reason, "previous_received_us": store.cursor("stream:" + coin)},
                quality="partial",
            )

    try:
        gap("stream_start_or_restart_gap")
        for attempt in range(3):
            if time.monotonic() >= deadline or messages >= max_messages:
                break
            try:
                with connect(
                    WS_URL,
                    open_timeout=min(5, max(0.1, deadline - time.monotonic())),
                    close_timeout=1,
                    max_size=1024 * 1024,
                    max_queue=16,
                    ping_interval=10,
                    ping_timeout=5,
                    proxy=None,
                    compression=None,
                ) as ws:
                    for coin in coins:
                        for kind in ("l2Book", "trades"):
                            ws.send(
                                canonical(
                                    {
                                        "method": "subscribe",
                                        "subscription": {"type": kind, "coin": coin},
                                    }
                                )
                            )
                    while time.monotonic() < deadline and messages < max_messages:
                        try:
                            raw = ws.recv(timeout=min(1, max(0.01, deadline - time.monotonic())))
                        except TimeoutError:
                            continue
                        messages += 1
                        if len(raw) > 1024 * 1024:
                            raise ValueError("stream_message_budget")
                        inserted += ingest_message(
                            store, json.loads(raw), coins=coins, received_us=time.time_ns() // 1000
                        )
                break
            except (OSError, WebSocketException, ValueError, KeyError, TypeError) as error:
                failures.append(type(error).__name__)
                gap("stream_disconnect_or_invalid_message")
                # Bounded reconnect and fresh book subscription; no fabricated
                # continuous tape or forward-filled missing book interval.
                if attempt < 2 and time.monotonic() < deadline:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        gap("stream_capture_stopped")
        return {
            **store.manifest(),
            "inserted_market_events": inserted,
            "messages": messages,
            "failures": failures,
            "budget_seconds": seconds,
            "max_messages": max_messages,
            "limitations": [
                "no trade continuity proof across disconnects",
                "books are snapshots",
                "publication time approximated by event time",
                "public trade participant addresses omitted",
            ],
        }
    finally:
        store.close()
