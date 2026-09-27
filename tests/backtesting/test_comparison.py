from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting import comparison
from liquid_autonomous_trader.backtesting.history import Minute


def test_study_resume_binds_economics_and_rejects_modified_artifact(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "manifest.json").write_text("{}", encoding="utf-8")
    (cache / "dataset.json").write_text("{}", encoding="utf-8")
    first = comparison.stamp(2025, 7, 2)
    last = first + 30 * comparison.MINUTE_US
    plan = comparison.declaration()
    plan.update(matched_window=[first, last], folds=[])
    monkeypatch.setattr(comparison, "declaration", lambda: plan)

    def minutes(root, start, end):
        for at in range(start, end, comparison.MINUTE_US):
            yield Minute(at, D(100000), D(100001), D(99999), D(100000), D(10), "fixture")

    monkeypatch.setattr("liquid_autonomous_trader.backtesting.historical.minutes", minutes)
    monkeypatch.setattr(
        "liquid_autonomous_trader.backtesting.historical.funding_rates", lambda _: {}
    )
    directory = tmp_path / "study"
    first_run = comparison.run(directory, cache, limit=1)
    assert first_run["counts"] == {"passed": 1, "failed": 0}
    assert first_run["runs"][0]["metrics"]["net_pnl"] == "0"
    resumed = comparison.run(directory, cache, limit=1)
    assert resumed["invocation"]["reused"] == 1
    assert resumed["invocation"]["executed"] == 1
    assert resumed["runs"][0] == first_run["runs"][0]
    artifact = directory / first_run["runs"][0]["artifact"]
    artifact.write_text("{}", encoding="utf-8")
    damaged = comparison.run(directory, cache, limit=1)
    assert damaged["stopped"] == "resumed_study_artifact_corrupted"
    assert damaged["counts"]["passed"] == 0
    with pytest.raises(ValueError, match="positive_task_limit"):
        comparison.run(directory, cache, limit=0)
