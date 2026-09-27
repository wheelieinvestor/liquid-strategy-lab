import hashlib
import json
import zipfile
from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting.history import (
    archive_minutes,
    funding_rates,
    verified_archives,
)
from liquid_autonomous_trader.backtesting.ledger import HOUR_US, Instrument, Ledger, Tier
from liquid_autonomous_trader.backtesting.report import metrics, render


def test_archive_checksums_and_contiguous_decimal_rows(tmp_path):
    relative = "daily/klines/BTCUSDT/1m/fixture.zip"
    archive = tmp_path / "archives" / relative
    archive.parent.mkdir(parents=True)
    rows = "0,100.0001,101,99,100.1,2\n60000,100.1,102,100,101,3\n"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("fixture.csv", rows)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.CHECKSUM").write_text(checksum + " fixture.zip")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "url": "https://data.binance.vision/data/futures/um/" + relative,
                        "sha256": checksum,
                        "rows": 2,
                    }
                ]
            }
        )
    )
    verified = list(verified_archives(tmp_path))
    values = list(archive_minutes(verified[0][0], checksum))
    assert values[0].open == D("100.0001") and values[1].open_us == 60_000_000
    archive.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        list(verified_archives(tmp_path))


def test_funding_preserves_actual_native_timestamp_and_rejects_corruption(tmp_path):
    dataset = b'{"bars":[],"funding":[{"rate":0.0001,"t":3600035}],"marks":[]}'
    (tmp_path / "dataset.json").write_bytes(dataset + b"\n")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"dataset_sha256": hashlib.sha256(dataset).hexdigest()})
    )
    assert funding_rates(tmp_path) == {3_600_035_000: D(".0001")}
    (tmp_path / "dataset.json").write_bytes(dataset.replace(b"0.0001", b"0.0002"))
    with pytest.raises(ValueError, match="checksum"):
        funding_rates(tmp_path)


def test_real_settlement_delay_changes_eligible_inventory_and_reconciles():
    spec = Instrument(
        "BTC", D(".01"), D(".01"), D(10), (Tier(D(0), D(40)),), "test", "synthetic_assumption"
    )
    book = Ledger(D(1000), {"BTC": spec})
    book.apply(
        "entry",
        HOUR_US,
        "fill",
        symbol="BTC",
        owner="btc_momentum",
        quantity="1",
        price="100",
        fee="0",
        leverage="10",
        mode="cross",
    )
    book.apply(
        "settlement",
        HOUR_US + 35000,
        "funding",
        symbol="BTC",
        oracle_price="100",
        rate=".001",
        settlement_us=HOUR_US + 35000,
        native_timestamp=True,
    )
    assert book.cash() == D("999.9")
    assert Ledger.replay(book.initial_cash, book.instruments, book.journal).state() == book.state()


def empty_result():
    return {
        "ledger": {
            "initial_cash": "1000",
            "cash": "1000",
            "equity": "1000",
            "realized": "0",
            "fees": "0",
            "funding": "0",
            "slippage": "0",
            "turnover": "0",
        },
        "start_us": 0,
        "end_us": 1000000,
        "equity_curve": [],
        "ledger_journal": [],
        "execution": {"events": [], "orders": {}},
        "breaches": [],
        "coverage": {"exact_jev_responses": 0, "missing_jev_reviews": 0},
        "rejection_counts": {},
        "limitations": ["<script>bad()</script>"],
        "verdict": "unsupported",
        "fidelity": "fixture",
        "mode": "synthetic_stress",
        "core_hash": "fixture",
    }


def test_zero_trade_report_and_html_escaping(tmp_path):
    result = empty_result()
    values = metrics(result)
    assert values["net_pnl"] == "0" and values["win_rate"] is None
    assert values["confidence_interval"]["status"] == "insufficient_data"
    path = tmp_path / "report.html"
    render(result, path)
    assert "<script>" not in path.read_text()
    assert "&lt;script&gt;" in path.read_text()
    result["ledger"]["fees"] = "1"
    with pytest.raises(ValueError, match="reconciliation"):
        metrics(result)
