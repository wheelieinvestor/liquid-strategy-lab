from liquid_autonomous_trader.backtesting.events import Catalog
from liquid_autonomous_trader.backtesting.recorder import capture, public_info


def test_recorder_restart_preserves_first_arrival_and_marks_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr("liquid_autonomous_trader.backtesting.recorder.time.sleep", lambda _: None)

    def fetch(body):
        kind = body["type"]
        if kind == "candleSnapshot":
            return [{"T": 899999, "c": "100"}]
        if kind == "fundingHistory":
            return [{"time": 3600000, "fundingRate": "0.0001"}]
        if kind == "l2Book":
            return {"time": 3600000, "levels": []}
        return {"metadata": "fixture"}

    path = tmp_path / "capture.db"
    first = capture(path, fetch=fetch)
    second = capture(path, fetch=fetch)
    assert first["failed_kinds"] == second["failed_kinds"] == []
    store = Catalog(path)
    rows = list(store.replay(until_us=2**62))
    assert len([e for e in rows if e.kind == "candle15m"]) == 1
    assert len([e for e in rows if e.kind == "book"]) == 1
    assert len([e for e in rows if e.kind == "gap"]) == 2
    assert any(e.payload.get("reason") == "restart_observation_gap" for e in rows)


def test_account_or_order_requests_are_unreachable():
    import pytest

    for kind in ["order", "exchange", "userFills", "clearinghouseState"]:
        with pytest.raises(ValueError, match="unsupported_public"):
            public_info({"type": kind})


def test_capture_failure_is_preserved_and_does_not_stop_other_lanes(tmp_path, monkeypatch):
    monkeypatch.setattr("liquid_autonomous_trader.backtesting.recorder.time.sleep", lambda _: None)

    def fail(_):
        raise OSError("fixture outage")

    path = tmp_path / "capture.db"
    result = capture(path, fetch=fail)
    assert len(result["failed_kinds"]) == 4
    assert result["event_count"] == 5
