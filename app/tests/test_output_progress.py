from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config.loader import PlanerConfig
from app.output.progress import BroadcastStep, ConsoleProgress, NoProgress, RunProgress
from app.pipeline.plan import PlannedBroadcast
from app.tests.conftest import build_config, build_planned, build_slot


def _item(now: datetime) -> PlannedBroadcast:
    config: PlanerConfig = build_config()
    return build_planned(build_slot(now + timedelta(days=1), "uk"), config.channels[0])


def _every_step(progress: RunProgress, item: PlannedBroadcast) -> None:
    progress.packages_read(2, 14, 10)
    progress.channel_read_started(item.channel)
    progress.channel_read_done(item.channel, 3)
    progress.broadcast_step_started(item, BroadcastStep.CREATE)
    progress.broadcast_step_started(item, BroadcastStep.FIX)
    progress.key_send_started(item)
    progress.report_started()


def test_no_progress_prints_nothing(now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    _every_step(NoProgress(), _item(now))
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_console_progress_prints_one_indented_line_per_step(now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    item: PlannedBroadcast = _item(now)
    _every_step(ConsoleProgress(), item)
    when: str = f"{item.date} {item.time} uk"
    assert capsys.readouterr().out.splitlines() == [
        "  пакетов прочитано 2: слотов 14, из них под мои языки 10",
        "  канал «yt_ua» @yt_ua: запрашиваю запланированные эфиры",
        "  канал «yt_ua» @yt_ua: запланированных эфиров 3",
        f"  канал «yt_ua» @yt_ua: создаю эфир {when}",
        f"  канал «yt_ua» @yt_ua: исправляю эфир {when}",
        f"  канал «yt_ua» @yt_ua: отправляю ключ в форму — эфир {when}",
        "  пишу отчёт",
    ]


def test_console_progress_flushes_every_line(now: datetime, monkeypatch: pytest.MonkeyPatch) -> None:
    """В собранном exe без flush строки копились бы до конца запуска."""
    flushes: list[bool] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: flushes.append(kwargs.get("flush", False)))
    _every_step(ConsoleProgress(), _item(now))
    assert flushes == [True] * 7
