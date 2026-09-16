from __future__ import annotations

import random
import re

import pytest
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import FormState, OutcomeKind, PackageLineStatus, SkipKind, SkippedLine
from app.paths import PlanerPaths
from app.output.console import render_console
from app.pipeline.plan import ChangedField, Decision, PlannedBroadcast
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.form.base import FORM_CODE_NOT_CONFIRMED, FormSendResult
from app.platforms.base import (
    PLACEHOLDER_TOKEN,
    BroadcastFacts,
    PlatformError,
    UpcomingBroadcast,
    VideoFixes,
    picture_sha,
)
from app.platforms.fake import FakePlatform
from app.output.progress import BroadcastStep
from app.tests.conftest import FORM_SPEC, FakeFormSender, RecordingProgress
from app.ui import messages_ru as msg

PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]
ConfigFactory = Callable[..., PlanerConfig]
UK_SLOT: str = "17-03-2027_1900_uk"
UK_START: datetime = datetime.fromisoformat("2027-03-17T19:00:00+02:00")
PLATFORM_KEY: str = "abcd-abcd-abcd-abcd-abcd"


class _PartialFormSender:
    """Подтверждает форму одному слоту и не подтверждает другому (§7.5)."""

    def __init__(self, confirmed_slot_id: str) -> None:
        self._confirmed_slot_id: str = confirmed_slot_id
        self.calls: list[str] = []

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        self.calls.append(planned.slot_id)
        if planned.slot_id == self._confirmed_slot_id:
            return FormSendResult(confirmed=True)
        return FormSendResult(
            confirmed=False,
            code=FORM_CODE_NOT_CONFIRMED,
            error="HTTP 200",
            diagnostic_path=Path("logs") / "16-03-2027_120000_form_response_abcdef12.html",
        )

def _run(
    mode: RunMode,
    paths: PlanerPaths,
    config: PlanerConfig,
    platform: FakePlatform,
    sender: FakeFormSender,
    now: datetime,
    rng: random.Random,
) -> RunOutcome:
    return run(mode, config, paths, platform, sender, now, rng)


def _report_text(outcome: RunOutcome) -> str:
    """Текст отчёта — из файла, который записал production-путь."""
    assert outcome.report_path is not None
    return outcome.report_path.read_text(encoding="utf-8")


def _kept_key_lines(outcome: RunOutcome) -> list[str]:
    assert outcome.report is not None
    return [line for line in outcome.report.warnings if line == msg.WARNING_KEPT_KEY]


def test_full_create_registers_and_confirms_form(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=2)])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    [created] = fake_platform.created
    [thumbnail] = fake_platform.thumbnails          # превью ставится отдельным шагом (§7.4 п.4)
    assert thumbnail.preview == b"x"
    assert fake_platform.languages == {created.broadcast_id: "uk"}
    [form_call] = form_sender.calls
    assert (form_call.slot_id, form_call.account_name, form_call.form_url) == (UK_SLOT, "yt_ua", FORM_SPEC["url"])
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "fake-0001-0000-0000-0000" in keys_text and "форма  отправлен в форму " in keys_text
    assert outcome.report is not None
    [pair_outcome] = outcome.report.outcomes
    assert (pair_outcome.kind, pair_outcome.form) == (OutcomeKind.CREATED, FormState.SENT)
    assert "эфир создан, ключ передан в форму" in _report_text(outcome)
    assert outcome.report_path is not None and outcome.report_path.exists()


def test_full_create_failure_is_an_error_and_sends_nothing(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_create[UK_SLOT] = PlatformError("liveStreamingNotEnabled", "на канале не включены трансляции")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.ERROR
    assert (
        f"YouTube: {msg.YOUTUBE_REASON_TEXT['liveStreamingNotEnabled']} (liveStreamingNotEnabled)"
        in _report_text(outcome)
    )
    assert form_sender.calls == []


def test_failed_form_is_reported_and_never_retried(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """Новый ключ не дошёл — код 1; следующий запуск видит тот же эфир с тем же ключом и в форму не шлёт."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    sender: FakeFormSender = FakeFormSender(confirmed=False, error="ошибка сети")
    first: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert first.exit_code == ExitCode.ERRORS
    assert first.report is not None and first.report.outcomes[0].form is FormState.FAILED
    assert "форма  НЕ отправлен: отправка не удалась (ошибка сети) — передайте стримеру вручную" in planer_paths.keys_file.read_text(encoding="utf-8")
    assert "повторно планер его не отправит" in _report_text(first)
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert len(sender.calls) == 1                      # повтора нет: ключ прежний
    assert second.exit_code == ExitCode.OK
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert msg.KEY_FORM_KEPT in planer_paths.keys_file.read_text(encoding="utf-8")


def test_processed_package_stays_in_bcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Планер пакеты не двигает и не удаляет: обработанный остаётся там же (§7.1)."""
    path: Path = make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("15-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "hu"), make_slot("17-03-2027", "19:00", "uk")],
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert path.exists()
    assert outcome.report is not None and outcome.report.packages[0].status is PackageLineStatus.ACCEPTED


def test_each_slot_uses_the_form_url_of_its_own_package(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Ссылка на форму — только из пакета: рядом лежащие пакеты могут указывать на разные формы."""
    old_url: str = "https://forms.gle/OldFormAAA"
    new_url: str = "https://forms.gle/NewFormBBB"
    make_package(
        planer_paths.bcast_dir,
        generated_at="13-09-2026 10:15",
        file_name="old.bcast",
        form={**FORM_SPEC, "url": old_url},
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk")],
    )
    make_package(
        planer_paths.bcast_dir,
        generated_at="14-09-2026 09:00",
        file_name="new.bcast",
        form={**FORM_SPEC, "url": new_url},
        slots=[make_slot("18-03-2027", "19:00", "uk")],
    )
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    urls: dict[str, str] = {call.slot_id: call.form_url for call in form_sender.calls}
    assert urls == {"17-03-2027_1900_uk": old_url, "18-03-2027_1900_uk": new_url}


def test_too_late_slot_keeps_package_in_bcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    path: Path = make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("16-03-2027", "12:30", "uk"), make_slot("17-03-2027", "19:00", "uk")],
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert path.exists()
    assert outcome.report is not None
    assert SkippedLine(
        SkipKind.TOO_LATE, "16-03-2027", "12:30", "uk", minutes=60, title="Эфир 16-03-2027_1230_uk"
    ) in outcome.report.skipped


def test_missing_broadcast_is_a_plain_create(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Эфира на площадке нет: обычное создание, без «заново» и без истории id."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "recreate" not in {decision.value for decision in Decision}
    [form_call] = form_sender.calls                    # новый ключ — ровно одна отправка
    assert form_call.stream_key == "fake-0001-0000-0000-0000"
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert "создан заново" not in _report_text(outcome)


def test_matched_key_comes_from_the_platform(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, stream_key=PLATFORM_KEY
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert PLATFORM_KEY in keys_text
    assert msg.KEY_FORM_KEPT in keys_text
    assert form_sender.calls == [] and fake_platform.created == []
    assert outcome.report is not None
    [pair_outcome] = outcome.report.outcomes
    assert (pair_outcome.kind, pair_outcome.form) == (OutcomeKind.MATCHED, None)


def test_matched_broadcast_is_never_sent_to_the_form(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Эфир с нашей меткой совпал с пакетом: ключ уже уходил раньше, в этом запуске его не шлём."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    fake_platform.seed_broadcast("yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert form_sender.calls == []
    assert outcome.exit_code == ExitCode.OK            # отсутствие отправки по совпавшему эфиру — не ошибка
    assert len(_kept_key_lines(outcome)) == 1
    assert msg.WARNING_KEPT_KEY in _report_text(outcome)


def test_two_packages_with_one_slot_give_one_object_per_channel(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Слоты сливаются по slot_id ДО размножения по каналам: два пакета — один эфир."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, file_name="old.bcast", generated_at="13-09-2026 10:15", slots=[spec])
    newer: dict[str, Any] = dict(spec, title="Новое название")
    make_package(planer_paths.bcast_dir, file_name="new.bcast", generated_at="14-09-2026 09:00", slots=[newer])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert len(outcome.report.outcomes) == 1
    assert len(fake_platform.created) == 1
    [created] = fake_platform.created
    assert created.marker == UK_SLOT


def test_broadcast_without_stream_is_reported_and_gets_no_key(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Эфир есть, потока нет: планер привязывает поток и получает ключ (§7.3)."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=None
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.created == []                      # эфир не пересоздавался
    assert [call.broadcast_id for call in fake_platform.attached] == [found.broadcast_id]
    assert len(form_sender.calls) == 1
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.STREAM_ATTACHED
    assert "поток привязан" in _report_text(outcome)


def test_package_fields_of_the_object_survive_the_whole_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Поля из пакета задаются в конструкторе и не переприсваиваются ни сверкой, ни действиями."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    # эфир без потока: привязка даёт новый ключ, значит объект дойдёт до формы
    fake_platform.seed_broadcast("yt_ua", UK_START, "Другое название", "Другое описание", marker=None)
    captured: list[PlannedBroadcast] = []
    original_send = FakeFormSender.send

    def _capture(self: FakeFormSender, planned: PlannedBroadcast) -> Any:
        captured.append(planned)
        return original_send(self, planned)

    monkeypatched: Any = _capture
    FakeFormSender.send = monkeypatched          # type: ignore[method-assign]
    try:
        _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    finally:
        FakeFormSender.send = original_send      # type: ignore[method-assign]
    [item] = captured
    assert item.slot.slot_id == UK_SLOT
    assert item.slot.title == spec["title"]                    # слот не переписан текстами с площадки
    assert item.expected.title == spec["title"]
    assert item.channel.account_name == "yt_ua"
    assert item.source_package.path.name.endswith(".bcast")
    assert item.actual is not None and item.actual.title == "Другое название"

def test_too_late_slot_reads_its_key_from_the_platform_and_writes_nothing(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Слот внутри min_lead_minutes: ключ с площадки остаётся в keys.txt, действий нет (§5.5, §7.2)."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("16-03-2027", "12:30", "uk")])
    soon_start: datetime = datetime.fromisoformat("2027-03-16T12:30:00+02:00")
    fake_platform.seed_broadcast(
        "yt_ua", soon_start, "Другое название", "", marker="16-03-2027_1230_uk", stream_key="soon-soon-soon-soon-soon"
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "soon-soon-soon-soon-soon" in keys_text
    assert fake_platform.created == [] and fake_platform.updated == [] and fake_platform.attached == []
    assert fake_platform.settings_calls == [] and fake_platform.facts_calls == [] and fake_platform.thumbnails == []
    assert form_sender.calls == []
    assert fake_platform.stream_calls                  # площадку прочитали — только чтение
    assert outcome.report is not None and outcome.report.outcomes == []
    assert "до старта меньше 60 минут" in _report_text(outcome)
    assert _kept_key_lines(outcome) == []


def test_too_late_slot_without_broadcast_has_no_key_row(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("16-03-2027", "12:30", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.created == [] and form_sender.calls == []
    lines: list[str] = planer_paths.keys_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(msg.KEYS_FILE_HEADER) and all(line.startswith("# ") for line in lines)


def test_thumbnail_failure_is_a_warning_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=1)])
    fake_platform.fail_thumbnail["fakebc00001"] = PlatformError("forbidden", "канал не подтверждён")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert outcome.report is not None and len(outcome.report.warnings) == 1
    assert "обложка не поставлена" in _report_text(outcome)
    assert msg.THUMBNAIL_REASON_TEXT["forbidden"] in _report_text(outcome)


def test_thumbnail_upload_limit_reaches_console_and_report_as_limit_text(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=1)])
    fake_platform.fail_thumbnail["fakebc00001"] = PlatformError("uploadRateLimitExceeded", "HTTP 429: limit")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK and outcome.report is not None
    console: str = render_console(outcome.report, root=planer_paths.root)
    for text in (console, _report_text(outcome)):
        assert msg.THUMBNAIL_REASON_TEXT["uploadRateLimitExceeded"] in text
        assert "подтверждённый канал" not in text


def test_video_settings_failure_is_a_warning_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_settings["fakebc00001"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "не удалось применить настройки эфира" in _report_text(outcome)


def test_language_is_set_from_the_slot(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "ru")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert fake_platform.languages == {"fakebc00001": "ru"}


def test_attach_failure_stays_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=None
    )
    fake_platform.fail_attach[found.broadcast_id] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert form_sender.calls == []


def test_second_run_matches_without_writes(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Второй прогон по тому же слоту: ни одного создания и исправления."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert len(fake_platform.created) == 1 and fake_platform.updated == []
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert len(form_sender.calls) == 1                                   # ключ прежний — повторной отправки нет
    assert outcome.exit_code == ExitCode.OK


def test_fix_keeps_key_and_url(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, "Старое название", spec["description"], marker=UK_SLOT,
        stream_key="abcd-abcd-abcd-abcd-abcd",
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "abcd-abcd-abcd-abcd-abcd" in keys_text and found.broadcast_id in keys_text
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.FIXED
    # исправили — ключ прежний, но стример получает его в этом запуске (решение 15-09-2026)
    assert [(call.slot_id, call.stream_key) for call in form_sender.calls] == [(UK_SLOT, "abcd-abcd-abcd-abcd-abcd")]
    assert outcome.exit_code == ExitCode.OK
    assert "исправлено, ключ и ссылка прежние, ключ передан в форму" in _report_text(outcome)


def test_one_failed_object_does_not_block_the_others(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    fake_platform.fail_create[UK_SLOT] = PlatformError("liveStreamingNotEnabled", "выключены")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert len(fake_platform.created) == 1

def test_made_for_kids_is_fixed_and_warned(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Настройка канала может перебить флаг: планер снимает его и говорит об этом владельцу."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.made_for_kids[found.broadcast_id] = True
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.made_for_kids[found.broadcast_id] is False
    assert "аудитория эфира была «для детей»" in _report_text(outcome)


def test_audience_failure_is_a_warning_too(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_settings["fakebc00001"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert outcome.report is not None and outcome.report.warnings


def test_age_restricted_broadcast_is_reported_but_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Возрастное ограничение через API не снимается — только сказать владельцу."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.age_restricted.add(found.broadcast_id)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "возрастное ограничение 18+" in _report_text(outcome)


def test_facts_are_read_once_per_object(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert len(fake_platform.facts_calls) == 2
    assert len(set(fake_platform.facts_calls)) == 2


def test_facts_are_not_read_for_too_late_and_ambiguous(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """too_late и AMBIGUOUS — не эфиры планера: их не трогают."""
    soon: dict[str, Any] = make_slot("16-03-2027", "12:30", "uk")
    ambiguous: dict[str, Any] = make_slot("18-03-2027", "19:00", "ru")
    make_package(planer_paths.bcast_dir, slots=[soon, ambiguous])
    start: datetime = datetime.fromisoformat("2027-03-18T19:00:00+02:00")
    fake_platform.seed_broadcast("yt_ru", start, "Ручной 1", "", marker=None)
    fake_platform.seed_broadcast("yt_ru", start, "Ручной 2", "", marker="Мой поток")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert fake_platform.facts_calls == []
    assert fake_platform.settings_calls == []
    assert outcome.report is not None


def test_matching_broadcast_has_no_mismatch_section(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.mismatches == []
    assert "Расхождения с платформой" not in _report_text(outcome)


def test_full_match_reports_no_mismatch_at_all(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Регрессия на живой случай 13-09-2026: время, тексты и маркер совпали — расхождений нет."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    facts = fake_platform.read_facts(make_config().channels[0], created.broadcast_id)
    assert facts.start_utc == UK_START.astimezone(timezone.utc)
    assert facts.stream_marker == UK_SLOT
    assert outcome.report is not None and outcome.report.mismatches == []
    assert "Расхождения с платформой" not in _report_text(outcome)


def test_description_mismatch_is_reported_shortened(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Описание в отчёт целиком не выводится: длина и начало."""
    long_text: str = "Очень длинное описание эфира. " * 40
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk", description=long_text)
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], long_text, marker=UK_SLOT,
    )
    # у ресурса видео описание другое — в списке эфиров этого не видно
    fake_platform.facts_override[found.broadcast_id] = BroadcastFacts(
        broadcast_id=found.broadcast_id,
        title=spec["title"],
        description="Совсем другое описание",
        start_utc=UK_START,
        privacy_status="public",
        made_for_kids=False,
        age_restricted=False,
        default_language="uk",
        default_audio_language="uk",
        category_id="22",
        bound_stream_id="fakestream0001",
        stream_marker=UK_SLOT,
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    text: str = _report_text(outcome)
    assert "Расхождения с платформой" in text
    assert "описание — хотели:" in text
    assert long_text.strip() not in text          # целиком не выводится


def test_expected_and_actual_log_lines_share_keys(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Наборы ключей не должны разъезжаться: сравнивать строки иначе бессмысленно."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    with caplog.at_level("INFO"):
        _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    lines: dict[str, str] = {
        message.split(" ", 1)[0]: message
        for message in caplog.messages
        if message.startswith(("broadcast_expected", "broadcast_actual", "broadcast_facts"))
    }
    assert set(lines) == {"broadcast_expected", "broadcast_actual", "broadcast_facts"}
    keys: dict[str, list[str]] = {
        name: re.findall(r"(?:^| )([a-z_]+)=", message)
        for name, message in lines.items()
    }
    assert keys["broadcast_expected"] == keys["broadcast_actual"]
    assert keys["broadcast_facts"][:2] == keys["broadcast_expected"][:2]   # slot_id, channel
    assert "description_head" in keys["broadcast_expected"]
    assert "made_for_kids" in keys["broadcast_facts"]

def test_category_from_planer_json_is_used_everywhere(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Категория берётся из planer.json — одна на все каналы — и при создании, и при настройке ресурса видео."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(category_id="25"), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    assert fake_platform.categories[created.broadcast_id] == "25"


def test_set_thumbnail_false_in_planer_json_skips_preview(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=2)])
    _run(RunMode.FULL, planer_paths, make_config(set_thumbnail=False), fake_platform, form_sender, now, rng)
    assert len(fake_platform.created) == 1 and fake_platform.thumbnails == []


def test_video_resource_is_touched_once_per_broadcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Регрессия: раньше по каждому эфиру ресурс видео читался трижды."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    assert fake_platform.settings_calls == [created.broadcast_id]
    assert fake_platform.settings_writes == [created.broadcast_id]


def test_second_run_writes_no_video_settings(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Совпало всё — запись не делается: чтение есть, записи нет."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    writes_after_first: int = len(fake_platform.settings_writes)
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert len(fake_platform.settings_writes) == writes_after_first
    assert len(fake_platform.settings_calls) == 2      # прочитали оба раза


def test_category_mismatch_is_reported(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.facts_override[found.broadcast_id] = BroadcastFacts(
        broadcast_id=found.broadcast_id,
        title=spec["title"],
        description=spec["description"],
        start_utc=UK_START,
        privacy_status="public",
        made_for_kids=False,
        age_restricted=False,
        default_language="uk",
        default_audio_language="uk",
        category_id="24",
        bound_stream_id="fakestream0001",
        stream_marker=UK_SLOT,
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "категория — хотели: 22; на платформе: 24" in _report_text(outcome)


def test_live_chat_is_warned_once_per_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Чат отключается в Студии на весь канал, поэтому строка одна, а не по разу на эфир."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    first: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    for call in fake_platform.created:
        fake_platform.live_chat_ids[call.broadcast_id] = "CHAT"
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    chat_lines: list[str] = [line for line in outcome.report.warnings if "живой чат" in line]
    assert len(chat_lines) == 1
    # и строка про прежний ключ — одна на запуск, только когда есть совпавшие эфиры
    assert _kept_key_lines(first) == []
    assert len(_kept_key_lines(outcome)) == 1


def test_no_live_chat_no_warning(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert [line for line in outcome.report.warnings if "живой чат" in line] == []


def test_facts_without_start_give_no_time_mismatch(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Нет времени в фактах — сравнивать не с чем; прочерк владелец читал бы как расхождение."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.facts_override[found.broadcast_id] = BroadcastFacts(
        broadcast_id=found.broadcast_id,
        title=spec["title"],
        description=spec["description"],
        start_utc=None,
        privacy_status="public",
        made_for_kids=False,
        age_restricted=False,
        default_language="uk",
        default_audio_language="uk",
        category_id="22",
        bound_stream_id="fakestream0001",
        stream_marker=UK_SLOT,
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert [line for line in outcome.report.mismatches if "время старта" in line] == []

def test_unconfirmed_form_keeps_pending_and_exits_1(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """Два объекта: одному форма подтверждена, другому нет — код выхода 1, ключ не потерян."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    sender: _PartialFormSender = _PartialFormSender(confirmed_slot_id=UK_SLOT)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    text: str = _report_text(outcome)
    assert "эфир создан, ключ передан в форму" in text
    assert "форма не подтвердила запись ответа" in text
    assert "ответ формы сохранён для разбора" in text

def test_all_past_package_stays_and_is_reported(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    path: Path = make_package(planer_paths.bcast_dir, slots=[make_slot("14-03-2027", "19:00", "uk")])
    dry: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert path.exists()
    assert dry.report is not None and dry.report.packages[0].status is PackageLineStatus.ALL_PAST
    full: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert path.exists()
    assert full.report is not None and full.report.packages[0].status is PackageLineStatus.ALL_PAST


def test_dry_run_changes_nothing(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    path: Path = make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert path.exists()
    assert not planer_paths.keys_file.exists()
    assert fake_platform.created == [] and form_sender.calls == []
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert "эфира нет, будет создан — не выполнено (dry-run)" in _report_text(outcome)


def test_status_lists_marked_broadcasts_into_keys_file(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    fake_platform.seed_broadcast("yt_ua", UK_START, "Эфир", "", marker=UK_SLOT, stream_key="aaaa-aaaa-aaaa-aaaa-aaaa")
    fake_platform.seed_broadcast(
        "yt_ru",
        datetime.fromisoformat("2027-03-18T20:00:00+02:00"),
        "Эфир",
        "",
        marker="18-03-2027_2000_en",
        stream_key="bbbb-bbbb-bbbb-bbbb-bbbb",
    )
    fake_platform.seed_broadcast("yt_ru", UK_START, "Ручной", "")
    outcome: RunOutcome = _run(RunMode.STATUS, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    lines: list[str] = planer_paths.keys_file.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = planer_paths.keys_file.read_text(encoding="utf-8").split("\n\n")[1:]
    assert len(lines) == len(msg.KEYS_FILE_HEADER) + 2 * 6   # шапка и два блока: пустая строка, заголовок, 4 поля
    assert "  ключ   aaaa-aaaa-aaaa-aaaa-aaaa" in blocks[0] and msg.KEY_FORM_KEPT in blocks[0]
    assert "  ключ   bbbb-bbbb-bbbb-bbbb-bbbb" in blocks[1] and msg.KEY_FORM_KEPT in blocks[1]
    assert outcome.report is not None
    assert [item.kind for item in outcome.report.outcomes] == [OutcomeKind.MATCHED, OutcomeKind.MATCHED]


def test_empty_bcast_exits_3(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert (outcome.exit_code, outcome.problem) == (ExitCode.BCAST_EMPTY, RunProblem.BCAST_EMPTY)


# --- задача 5d: сверяется всё, что планер диктует площадке


def test_privacy_only_difference_is_fixed_and_key_goes_to_the_form(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Живой прогон 15-09: владелец поставил Private — планер возвращает видимость и шлёт ключ."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, stream_key=PLATFORM_KEY,
        privacy="private",
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert (pair.kind, pair.changed_fields, pair.form) == (OutcomeKind.FIXED, ("privacy",), FormState.SENT)
    assert [call.stream_key for call in form_sender.calls] == [PLATFORM_KEY]
    # исправляемый эфир переотправляется целиком: один liveBroadcasts.update и обложка из пакета
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    assert [call.broadcast_id for call in fake_platform.thumbnails] == [found.broadcast_id]
    [listed] = fake_platform.list_upcoming(make_config().channels[0])
    assert (listed.broadcast_id, listed.privacy_status) == (found.broadcast_id, "public")
    assert outcome.exit_code == ExitCode.OK


def test_category_fixed_on_the_video_counts_as_a_fix(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Категорию список эфиров не возвращает: её расхождение видно у ресурса видео — и это тоже исправление."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, category_id="24",
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert (pair.kind, pair.changed_fields, pair.form) == (OutcomeKind.FIXED, ("category",), FormState.SENT)
    assert len(form_sender.calls) == 1


def test_auto_start_difference_keeps_decision_but_reaches_owner(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Автостарт через API не исправить: решение MATCH, ключ не шлём, но лог, «внимание:» и расхождение в отчёте."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, auto_start=False,
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert pair.kind is OutcomeKind.MATCHED
    assert form_sender.calls == []
    warning: str = "не можем исправить: 17-03-2027 19:00 uk -> yt_ua — автостарт: нужно да, на площадке нет;"
    assert any(line.startswith(warning) for line in outcome.report.run_warnings)
    report_text: str = _report_text(outcome)
    assert "17-03-2027 19:00 uk -> yt_ua: автостарт — хотели: да; на платформе: нет" in report_text
    console: str = render_console(outcome.report, root=planer_paths.root, report_path=outcome.report_path)
    assert any(line.startswith(f"  {warning}") for line in console.splitlines())
    assert outcome.exit_code == ExitCode.OK


def test_manual_broadcast_is_adopted_marked_and_its_key_sent(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Ручной эфир без нашей метки: стример этого ключа не видел — метку ставим, ключ шлём."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    manual: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker="Мой поток", stream_key=PLATFORM_KEY,
    )
    first: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [(call.broadcast_id, call.marker) for call in fake_platform.markers_set] == [(manual.stream_id, UK_SLOT)]
    assert [call.stream_key for call in form_sender.calls] == [PLATFORM_KEY]
    assert first.report is not None and first.report.outcomes[0].changed_fields == ("marker",)
    assert fake_platform.created == []
    assert [call.broadcast_id for call in fake_platform.updated] == [manual.broadcast_id]   # переотправка целиком
    # со следующего запуска видно, что ключ уходил: наша метка, MATCH, ключ повторно не шлём
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert len(form_sender.calls) == 1


def test_two_unmarked_broadcasts_are_ambiguous_with_links(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Планер не выбирает и не удаляет, но называет дату, время, язык, канал и ссылки на всех кандидатов."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    first: UpcomingBroadcast = fake_platform.seed_broadcast("yt_ua", UK_START, "Ручной 1", "", marker=None)
    second: UpcomingBroadcast = fake_platform.seed_broadcast("yt_ua", UK_START, "Ручной 2", "", marker="Мой поток")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert pair.kind is OutcomeKind.AMBIGUOUS
    urls: str = (
        f"https://www.youtube.com/watch?v={first.broadcast_id}, https://www.youtube.com/watch?v={second.broadcast_id}"
    )
    [warning] = [line for line in outcome.report.run_warnings if "17-03-2027 19:00 uk -> yt_ua" in line]
    assert warning.endswith(urls)
    assert warning in _report_text(outcome)
    assert fake_platform.created == [] and fake_platform.markers_set == [] and form_sender.calls == []
    assert outcome.exit_code == ExitCode.ERRORS


def test_created_broadcast_has_no_marker_mismatch(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """14-09: факты не увидели только что привязанный поток — «маркер потока … на платформе: -» было ложным."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    real_read_facts = fake_platform.read_facts

    def _facts_without_stream(channel: Any, broadcast_id: str) -> BroadcastFacts:
        facts: BroadcastFacts = real_read_facts(channel, broadcast_id)
        return BroadcastFacts(**{**facts.__dict__, "bound_stream_id": None, "stream_marker": None})

    fake_platform.read_facts = _facts_without_stream  # type: ignore[method-assign]
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert not any("маркер потока" in line for line in outcome.report.mismatches)


def test_language_just_written_is_taken_from_the_write_response(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """15-09: язык uk записан, перечитывание через секунду отдало ru — расхождение было ложным."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    real_read_facts = fake_platform.read_facts

    def _facts_with_stale_language(channel: Any, broadcast_id: str) -> BroadcastFacts:
        facts: BroadcastFacts = real_read_facts(channel, broadcast_id)
        return BroadcastFacts(**{**facts.__dict__, "default_language": "ru", "default_audio_language": "ru"})

    fake_platform.read_facts = _facts_with_stale_language  # type: ignore[method-assign]
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert not any(msg.MISMATCH_FIELD_LANGUAGE in line for line in outcome.report.mismatches)


def test_language_refused_by_the_platform_is_still_a_mismatch(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Ответу записи верим, но сверяем с отправленным: вернули ru вместо uk — расхождение настоящее."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    real_apply = fake_platform.apply_video_settings

    def _apply_refusing_language(
        channel: Any, broadcast_id: str, language: str, category_id: str, privacy: str
    ) -> VideoFixes:
        fixes: VideoFixes = real_apply(channel, broadcast_id, language, category_id, privacy)
        assert fixes.applied is not None
        return replace(fixes, applied=replace(fixes.applied, language="ru", audio_language="ru"))

    fake_platform.apply_video_settings = _apply_refusing_language  # type: ignore[method-assign]
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [line] = [text for text in outcome.report.mismatches if msg.MISMATCH_FIELD_LANGUAGE in text]
    assert "хотели: uk" in line and "на платформе: ru" in line


def test_full_run_reports_progress_in_step_order(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Пакеты → чтение каналов → создание и исправление → ключи в форму → отчёт; числа — как в «Пакетах» отчёта."""
    ru_spec: dict[str, Any] = make_slot("18-03-2027", "19:00", "ru")
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), ru_spec, make_slot("18-03-2027", "19:00", "hu")],
    )
    fake_platform.seed_broadcast(
        "yt_ru", datetime.fromisoformat("2027-03-18T19:00:00+02:00"), "Старое название", ru_spec["description"],
        marker="18-03-2027_1900_ru", stream_key=PLATFORM_KEY,
    )
    progress: RecordingProgress = RecordingProgress()
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(), planer_paths, fake_platform, form_sender, now, rng, progress=progress
    )
    assert outcome.exit_code == ExitCode.OK
    assert progress.calls == [
        ("packages_read", 1, 3, 2),
        ("channel_read_started", "yt_ua"),
        ("channel_read_done", "yt_ua", 0),
        ("channel_read_started", "yt_ru"),
        ("channel_read_done", "yt_ru", 1),
        ("broadcast_step_started", UK_SLOT, "yt_ua", BroadcastStep.CREATE),
        ("broadcast_step_started", "18-03-2027_1900_ru", "yt_ru", BroadcastStep.FIX),
        ("key_send_started", UK_SLOT, "yt_ua"),
        ("key_send_started", "18-03-2027_1900_ru", "yt_ru"),
        ("report_started",),
    ]
    assert outcome.report is not None
    assert f"{outcome.report.packages[0].file_name} — принят, слотов 3, из них под мои языки 2" in _report_text(outcome)


def test_dry_run_progress_has_no_action_steps(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    progress: RecordingProgress = RecordingProgress()
    run(RunMode.DRY_RUN, make_config(), planer_paths, fake_platform, form_sender, now, rng, progress=progress)
    assert progress.names() == ["packages_read", "channel_read_started", "channel_read_done", "report_started"]


def test_status_progress_reads_every_channel_and_the_report(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    progress: RecordingProgress = RecordingProgress()
    run(RunMode.STATUS, make_config(), planer_paths, fake_platform, form_sender, now, rng, progress=progress)
    assert progress.calls == [
        ("channel_read_started", "yt_ua"),
        ("channel_read_done", "yt_ua", 0),
        ("channel_read_started", "yt_ru"),
        ("channel_read_done", "yt_ru", 0),
        ("report_started",),
    ]


def test_empty_bcast_reports_no_progress(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    progress: RecordingProgress = RecordingProgress()
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(), planer_paths, fake_platform, form_sender, now, rng, progress=progress
    )
    assert outcome.exit_code == ExitCode.BCAST_EMPTY and progress.calls == []


# --- задача 5k: исправляемый эфир переотправляется целиком, обложка — сверяемое поле


def _seed_uk(fake_platform: FakePlatform, spec: dict[str, Any], **overrides: Any) -> UpcomingBroadcast:
    """Эфир планера на канале yt_ua: тексты и метка — как в пакете."""
    values: dict[str, Any] = {"marker": UK_SLOT, "stream_key": PLATFORM_KEY}
    values.update(overrides)
    return fake_platform.seed_broadcast("yt_ua", UK_START, spec["title"], spec["description"], **values)


def _placeholder_overrides() -> dict[str, Any]:
    placeholder: str = FakePlatform.placeholder_of("yt_ua")
    return {"picture": placeholder, "stream_description": PLACEHOLDER_TOKEN.format(sha=placeholder)}


def test_missing_thumbnail_alone_resends_the_broadcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = _seed_uk(fake_platform, spec, **_placeholder_overrides())
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert (pair.kind, pair.changed_fields, pair.form) == (OutcomeKind.FIXED, ("thumbnail",), FormState.SENT)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    [thumbnail] = fake_platform.thumbnails
    assert (thumbnail.broadcast_id, thumbnail.preview) == (found.broadcast_id, b"x")   # превью слота из пакета
    assert [call.stream_key for call in form_sender.calls] == [PLATFORM_KEY]
    console: list[str] = render_console(outcome.report, root=planer_paths.root).splitlines()
    fixed: int = next(index for index, line in enumerate(console) if msg.CONSOLE_BLOCK_FIXED in line)
    assert any(line.endswith("— обновлено: обложка") for line in console[fixed:])
    assert not any("обложка" in line and "вернули" in line for line in console)   # не «вернули к пакету»
    [change] = pair.field_changes
    assert (change.name, change.before, change.after) == ("thumbnail", msg.THUMBNAIL_BEFORE, msg.THUMBNAIL_AFTER)
    # следующий запуск: картинка — уже своя обложка, эфир совпадает, ключ повторно не уходит
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert len(form_sender.calls) == 1


def test_title_fix_also_resends_the_thumbnail(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, "Старое название", spec["description"], marker=UK_SLOT, stream_key=PLATFORM_KEY,
        picture="aaaaaaaaaaaa",
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.outcomes[0].changed_fields == ("title",)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    assert [call.broadcast_id for call in fake_platform.thumbnails] == [found.broadcast_id]
    assert fake_platform.pictures[found.broadcast_id] == picture_sha(b"x")


def test_matching_broadcast_is_not_touched(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    _seed_uk(fake_platform, spec, picture="aaaaaaaaaaaa")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert fake_platform.updated == [] and fake_platform.thumbnails == [] and form_sender.calls == []


def test_privacy_fixed_at_video_resource_resends_the_broadcast_once(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Список эфиров видимость не вернул — MATCH; ресурс видео её исправил — UPDATE и одна переотправка."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = _seed_uk(fake_platform, spec, picture="aaaaaaaaaaaa", privacy=None)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert (pair.kind, pair.changed_fields, pair.form) == (OutcomeKind.FIXED, ("privacy",), FormState.SENT)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    assert [call.broadcast_id for call in fake_platform.thumbnails] == [found.broadcast_id]
    assert fake_platform.settings_calls == [found.broadcast_id]      # настройки видео — одним проходом


def test_upload_limit_on_resend_keeps_update_and_key(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = _seed_uk(fake_platform, spec, **_placeholder_overrides())
    fake_platform.fail_thumbnail[found.broadcast_id] = PlatformError("uploadRateLimitExceeded", "HTTP 429: limit")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK and outcome.report is not None
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    assert [call.stream_key for call in form_sender.calls] == [PLATFORM_KEY]
    assert any(msg.THUMBNAIL_REASON_TEXT["uploadRateLimitExceeded"] in line for line in outcome.report.warnings)
