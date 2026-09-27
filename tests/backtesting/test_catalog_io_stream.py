import json
from dataclasses import asdict

import pytest

from liquid_autonomous_trader.backtesting.catalog_io import (
    export_catalog,
    ingest_spool,
    verify_export,
)
from liquid_autonomous_trader.backtesting.events import Catalog, Event, canonical
from liquid_autonomous_trader.backtesting.stream import capture_stream, ingest_message


def source_event(sequence=1):
    return Event(
        source="test",
        instrument="QQQ",
        venue="source",
        kind="gamma_matrix",
        event_us=100,
        published_us=100,
        available_us=120,
        retrieved_us=130,
        sequence=sequence,
        revision=0,
        units="fixture",
        quality="synthetic_assumption",
        reference="test",
        payload_json=canonical({"revision": sequence}),
    )


def test_spool_restart_partial_line_and_changed_prefix(tmp_path):
    store = Catalog(tmp_path / "research.sqlite")
    path = tmp_path / "source.jsonl"
    first = (canonical(asdict(source_event())) + "\n").encode()
    second = (canonical(asdict(source_event(2))) + "\n").encode()
    path.write_bytes(first + second[:-1])
    assert ingest_spool(store, path)["inserted"] == 1
    assert ingest_spool(store, path)["inserted"] == 0
    path.write_bytes(first + second)
    assert ingest_spool(store, path)["inserted"] == 1
    path.write_bytes(first.replace(b"QQQ", b"BAD") + second)
    with pytest.raises(ValueError, match="prefix_changed"):
        ingest_spool(store, path)
    assert store.manifest()["event_count"] == 2
    store.close()


def test_export_is_verified_and_never_overwritten(tmp_path):
    store = Catalog(tmp_path / "research.sqlite")
    store.append([source_event()])
    path = tmp_path / "events.jsonl"
    manifest = export_catalog(store, path)
    assert verify_export(path) == manifest
    restored = Catalog(tmp_path / "restored.sqlite")
    for line in path.read_text().splitlines():
        value = json.loads(line)
        value["lineage"] = tuple(value["lineage"])
        restored.append([Event(**value)])
    assert restored.manifest() == store.manifest()
    with pytest.raises(ValueError, match="destination_exists"):
        export_catalog(store, path)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="size"):
        verify_export(path)
    restored.close()
    store.close()


def test_trade_identity_dedupe_and_participant_privacy(tmp_path):
    store = Catalog(tmp_path / "research.sqlite")
    row = {
        "coin": "BTC",
        "time": 1,
        "tid": 1,
        "px": "100",
        "sz": "2",
        "side": "B",
        "users": ["0x" + "a" * 40, "0x" + "b" * 40],
    }
    message = {"channel": "trades", "data": [row, dict(row, tid=2)]}
    assert ingest_message(store, message, coins=("BTC",), received_us=2000) == 2
    assert ingest_message(store, message, coins=("BTC",), received_us=3000) == 0
    saved = list(store.replay(until_us=5000))
    assert all("users" not in e.payload for e in saved)
    assert {e.sequence for e in saved} == {1, 2}
    assert {e.available_us for e in saved} == {2000}
    with pytest.raises(ValueError, match="unsubscribed_stream_channel"):
        ingest_message(
            store, {"channel": "userFills", "data": []}, coins=("BTC",), received_us=3000
        )
    store.close()


def test_stream_reconnect_is_bounded_and_gaps_preserved(tmp_path, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise OSError("fixture")

    monkeypatch.setattr("liquid_autonomous_trader.backtesting.stream.time.sleep", lambda _: None)
    result = capture_stream(tmp_path / "research.sqlite", connect=fail, seconds=1)
    assert len(calls) == 3
    assert calls[0]["max_queue"] == 16
    assert result["failures"] == ["OSError"] * 3
    assert result["event_count"] == 5
