from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.observability.run_stats import OTHER_STAGE, RunKind, RunStage, RunStats


class _Clock:
    def __init__(self) -> None:
        self.now: float = 100.0

    def __call__(self) -> float:
        return self.now


def _stats(clock: _Clock) -> RunStats:
    return RunStats(
        kind=RunKind.DRY_RUN,
        version="9.9.9",
        clock=clock,
        started_utc=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc),
    )


def test_methods_are_counted_per_operation() -> None:
    stats: RunStats = _stats(_Clock())
    stats.request_done("videos.list", 0.25, 1)
    stats.request_done("videos.list", 0.5, 1)
    stats.retried("videos.list", 2.4)
    stats.empty_answer("videos.list")
    stats.request_done("liveBroadcasts.insert", 1.0, 50)
    stats.refused("liveBroadcasts.insert")
    stats.paused(1.5)
    assert (stats.youtube_calls, stats.youtube_units) == (3, 52)
    assert stats.request_sec == pytest.approx(1.75)
    videos = stats.methods["videos.list"]
    assert (videos.calls, videos.retries, videos.empty, videos.refusals, videos.units) == (2, 1, 1, 0, 2)
    assert stats.methods["liveBroadcasts.insert"].refusals == 1
    assert (stats.pause_sec, stats.retry_sleep_sec) == (1.5, 2.4)


def test_stages_add_up_and_other_is_the_rest() -> None:
    clock: _Clock = _Clock()
    stats: RunStats = _stats(clock)
    with stats.stage(RunStage.PACKAGES):
        clock.now += 2.0
    clock.now += 1.0          # вне этапов — «прочее»
    with stats.stage(RunStage.LOGINS):
        clock.now += 30.0
    with stats.stage(RunStage.PACKAGES):
        clock.now += 0.5
    with pytest.raises(RuntimeError):
        with stats.stage(RunStage.REPORT):
            clock.now += 0.25
            raise RuntimeError("этап оборвался — время всё равно учтено")
    stats.finish(1)
    clock.now += 100.0        # после finish время не идёт
    assert stats.stages == {RunStage.PACKAGES: 2.5, RunStage.LOGINS: 30.0, RunStage.REPORT: 0.25}
    assert stats.elapsed_sec == pytest.approx(33.75)
    assert stats.other_sec == pytest.approx(1.0)
    assert stats.exit_code == 1


def test_objects_keep_count_sum_and_max_per_decision() -> None:
    stats: RunStats = _stats(_Clock())
    stats.object_done("create", 20.0)
    stats.object_done("create", 26.0)
    stats.object_done("match", 3.0)
    create = stats.objects["create"]
    assert (create.count, create.total_sec, create.max_sec) == (2, 46.0, 26.0)
    assert stats.objects["match"].count == 1


def test_json_carries_everything_with_seconds_to_one_digit() -> None:
    clock: _Clock = _Clock()
    stats: RunStats = _stats(clock)
    stats.request_done("videos.list", 0.123, 1)
    stats.form_read(0.44)
    stats.form_posted(1.06)
    stats.picture_downloaded(0.31)
    stats.record_totals({"created": 2})
    clock.now += 61.26
    stats.finish(0)
    data = stats.to_json()
    assert (data["version"], data["mode"], data["exit_code"], data["elapsed_sec"]) == ("9.9.9", "dry_run", 0, 61.3)
    assert data["youtube"]["methods"]["videos.list"] == {
        "calls": 1, "retries": 0, "empty": 0, "refusals": 0, "request_sec": 0.1, "units": 1
    }
    assert data["form"] == {"reads": {"count": 1, "seconds": 0.4}, "posts": {"count": 1, "seconds": 1.1}}
    assert data["pictures"] == {"count": 1, "seconds": 0.3}
    assert data["stages"][OTHER_STAGE] == 61.3
    assert data["totals"] == {"created": 2}
    assert data["started"].count(":") == 2       # DD-MM-YYYY HH:MM:SS
