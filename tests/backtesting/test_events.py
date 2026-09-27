import sqlite3
from dataclasses import replace

import pytest

from liquid_autonomous_trader.backtesting.events import Catalog, Event, canonical


def event(**kwargs):
    return replace(
        Event(
            source="fixture",
            instrument="BTC-PERP",
            venue="test",
            kind="bar",
            event_us=10,
            published_us=11,
            available_us=12,
            retrieved_us=13,
            sequence=1,
            revision=0,
            units="USD",
            quality="synthetic_assumption",
            reference="fixture:one",
            payload_json=canonical({"close": "100"}),
        ),
        **kwargs,
    )


def test_causal_prefix_is_unchanged_by_future_and_revised_rows(tmp_path):
    store = Catalog(tmp_path / "research.db")
    original = event()
    store.append([original])
    before = list(store.replay(until_us=12))
    store.append(
        [
            event(event_us=20, published_us=21, available_us=22, retrieved_us=23),
            event(
                revision=1,
                available_us=30,
                retrieved_us=31,
                payload_json=canonical({"close": "99"}),
            ),
        ]
    )
    assert list(store.replay(until_us=12)) == before == [original]
    assert len(list(store.replay(until_us=30))) == 3


def test_conflicting_batch_rolls_back_cursor_and_events(tmp_path):
    store = Catalog(tmp_path / "research.db")
    store.append([event()], cursor=("fixture", "1"))
    with pytest.raises(ValueError, match="conflicting_revision"):
        store.append(
            [event(sequence=2), event(payload_json=canonical({"close": "90"}))],
            cursor=("fixture", "2"),
        )
    assert store.manifest()["event_count"] == 1
    assert store.cursor("fixture") == "1"


def test_restart_dedupes_and_ignores_retrieval_time(tmp_path):
    path = tmp_path / "research.db"
    store = Catalog(path)
    store.append([event()], cursor=("fixture", "1"))
    expected = store.manifest()
    store.close()
    restored = Catalog(path)
    assert restored.append([event(retrieved_us=100)]) == 0
    assert restored.manifest() == expected
    assert restored.cursor("fixture") == "1"
    with pytest.raises(sqlite3.IntegrityError, match="immutable_event"):
        restored.db.execute("DELETE FROM events")


def test_deterministic_ties_and_storage_order(tmp_path):
    a, b = Catalog(tmp_path / "a.db"), Catalog(tmp_path / "b.db")
    values = [event(sequence=2), event(sequence=1), event(sequence=0)]
    a.append(values)
    b.append(list(reversed(values)))
    assert a.manifest() == b.manifest()
    assert list(a.replay(until_us=20)) == list(b.replay(until_us=20))


def test_refuses_production_database_and_invalid_timing(tmp_path):
    path = tmp_path / "live.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE orders (id TEXT)")
    with pytest.raises(ValueError, match="not_a_research_catalog"):
        Catalog(path)
    with pytest.raises(ValueError, match="availability_precedes"):
        event(available_us=9)
    store = Catalog(tmp_path / "research.db", max_bytes=1)
    with pytest.raises(ValueError, match="disk_budget"):
        store.append([event()])


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": {"apiKey": "test-placeholder"}},
        {"wallet_address": "test-placeholder"},
        {"detail": "0x" + "a" * 40},
        {"url": "https://example.test/data?token=test-placeholder"},
        {"dsn": "postgresql://example.test/db"},
    ],
)
def test_raw_account_credentials_and_secret_urls_rejected_without_echo(payload):
    with pytest.raises(ValueError) as error:
        event(payload_json=canonical(payload))
    assert "test-placeholder" not in str(error.value)
    assert "a" * 40 not in str(error.value)
