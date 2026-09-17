from __future__ import annotations

import logging
import random
import re

import pytest
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.dates import format_datetime_text
from app.package.model import FormSpec, PackageError, PackageErrorReason
from app.output.report import FormState, OutcomeKind, PackageLineStatus, SkipKind, SkippedLine
from app.paths import PlanerPaths
from app.output.console import render_console
from app.pipeline.plan import ChangedField, Decision, PlannedBroadcast
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.form.base import FORM_CODE_NOT_CONFIRMED, FormSendResult
from app.form.discovery import FormDiscovery, FormStructure
from app.form.key_form import KeyForm
from app.platforms.base import (
    PLACEHOLDER_TOKEN,
    BroadcastFacts,
    PlatformError,
    UpcomingBroadcast,
    VideoFixes,
    picture_sha,
)
from app.platforms.channel import Channel, ChannelStatus
from app.platforms.fake import FakePlatform
from app.records.record_store import RecordStore
from app.records.slot_record import RecordResults, SlotRecord, SlotStage
from app.tests.conftest import FIXED_NOW
from app.output.progress import BroadcastStep
from app.tests.conftest import FORM_SPEC, FakeFormSender, RecordingProgress, build_form_spec
from app.tests.test_form_discovery import _FakeResponse, _FakeSession, build_html, build_payload, default_items
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

    def prepare(self, forms: Sequence[FormSpec]) -> None:
        """Формы в этом тесте не читаются."""

    def form_for(self, spec: FormSpec) -> None:
        """Форма не проверяется: объекты допускаются без неё."""
        return None

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
    store: RecordStore | None = None,
) -> RunOutcome:
    """Без store — пустая память первого запуска (эфиры с меткой планера — уже переданные). Часы стоят на now."""
    return run(mode, config, paths, platform, sender, now, rng, store=store, clock=lambda: now)


def _memory_without_confirmations() -> RecordStore:
    """Память есть, но подтверждений в ней нет: стоящие эфиры не считаются переданными стримеру."""
    return RecordStore.memory(is_new=False, created_utc=datetime(2025, 1, 1, tzinfo=timezone.utc))


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


def test_failed_form_is_reported_and_retried_next_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """Новый ключ не дошёл — код 1; подтверждения в памяти нет — следующий запуск отправляет его снова."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    sender: FakeFormSender = FakeFormSender(confirmed=False, error="ошибка сети")
    store: RecordStore = _memory_without_confirmations()
    first: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng, store)
    assert first.exit_code == ExitCode.ERRORS
    assert first.report is not None and first.report.outcomes[0].form is FormState.FAILED
    assert "форма  НЕ отправлен — отправка не удалась (ошибка сети); передайте стримеру вручную" in (
        planer_paths.keys_file.read_text(encoding="utf-8")
    )
    assert "следующий запуск отправит его снова" in _report_text(first)
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng, store)
    assert [call.stream_key for call in sender.calls] == ["fake-0001-0000-0000-0000"] * 2
    assert second.exit_code == ExitCode.ERRORS
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert second.report.outcomes[0].form is FormState.FAILED


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
    assert msg.KEY_FORM_BOOTSTRAP in keys_text            # первый запуск с памятью: эфир с меткой уже стоял
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
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "abcd-abcd-abcd-abcd-abcd" in keys_text and found.broadcast_id in keys_text
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.FIXED
    # исправили — ключ прежний; подтверждения в памяти нет — стример получает его в этом запуске
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
    assert "  ключ   aaaa-aaaa-aaaa-aaaa-aaaa" in blocks[0] and msg.KEY_FORM_UNKNOWN in blocks[0]
    assert "  ключ   bbbb-bbbb-bbbb-bbbb-bbbb" in blocks[1] and msg.KEY_FORM_UNKNOWN in blocks[1]
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
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
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
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
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
    warning: str = "не можем исправить: 17-03-2027 19:00 uk -> yt_ua @yt_ua — автостарт: нужно да, на площадке нет;"
    assert any(line.startswith(warning) for line in outcome.report.run_warnings)
    report_text: str = _report_text(outcome)
    assert "17-03-2027 19:00 uk -> yt_ua @yt_ua: автостарт — хотели: да; на платформе: нет" in report_text
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
    """Пакеты → чтение каналов → по объекту: действие и сразу его ключ → отчёт; числа — как в «Пакетах» отчёта."""
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
        RunMode.FULL, make_config(), planer_paths, fake_platform, form_sender, now, rng, progress=progress,
        store=_memory_without_confirmations(),
    )
    assert outcome.exit_code == ExitCode.OK
    assert progress.calls == [
        ("packages_read", 1, 3, 2),
        ("channel_read_started", "yt_ua"),
        ("channel_read_done", "yt_ua", 0),
        ("channel_read_started", "yt_ru"),
        ("channel_read_done", "yt_ru", 1),
        ("broadcast_step_started", UK_SLOT, "yt_ua", BroadcastStep.CREATE),
        ("key_send_started", UK_SLOT, "yt_ua"),
        ("broadcast_step_started", "18-03-2027_1900_ru", "yt_ru", BroadcastStep.FIX),
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
    store: RecordStore = _memory_without_confirmations()
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, store)
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
    # следующий запуск: картинка — уже своя обложка, эфир совпадает, подтверждение в памяти — ключ не уходит
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, store)
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
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
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
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
    assert outcome.exit_code == ExitCode.OK and outcome.report is not None
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    assert [call.stream_key for call in form_sender.calls] == [PLATFORM_KEY]
    assert any(msg.THUMBNAIL_REASON_TEXT["uploadRateLimitExceeded"] in line for line in outcome.report.warnings)


def test_channels_with_one_title_and_other_handles_do_not_mix(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: Callable[..., PlanerConfig],
    fake_platform: FakePlatform,
    form_sender: FakeFormSender,
    now: datetime,
    rng: random.Random,
) -> None:
    """Два канала «Українка» с разными никами: свои эфиры, свои потоки, свой отказ и своя группа в консоли."""
    base: PlanerConfig = make_config([("twin_a", ["uk"]), ("twin_b", ["uk"])])
    config: PlanerConfig = replace(
        base, channels=tuple(replace(channel, account_name="Українка") for channel in base.channels)
    )
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk")])
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    fake_platform.seed_broadcast("@twin_a", UK_START, spec["title"], spec["description"], marker=UK_SLOT)
    fake_platform.fail_list["twin_b"] = PlatformError("liveStreamingNotEnabled", "выключены")
    outcome: RunOutcome = run(RunMode.FULL, config, planer_paths, fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    kinds = sorted((item.handle, item.date or "", item.kind.value) for item in outcome.report.outcomes)
    assert kinds == [
        ("@twin_a", "17-03-2027", OutcomeKind.MATCHED.value),
        ("@twin_a", "18-03-2027", OutcomeKind.CREATED.value),
        ("@twin_b", "17-03-2027", OutcomeKind.ERROR.value),
        ("@twin_b", "18-03-2027", OutcomeKind.ERROR.value),
    ]
    assert [call.channel_id for call in fake_platform.created] == ["twin_a"]
    assert {channel for channel, _stream in fake_platform.stream_calls} == {"twin_a"}
    assert fake_platform.list_calls == ["twin_a", "twin_b"]
    console: str = render_console(outcome.report, root=planer_paths.root, channel_order=("twin_a", "twin_b"))
    lines: list[str] = console.splitlines()
    assert lines.count("  Українка @twin_a (owner@gmail.com)") == 3       # ОПУБЛИКОВАЛИ, КЛЮЧИ, УЖЕ СТОЯЛО
    assert "  Українка @twin_b (owner@gmail.com)" not in lines            # у twin_b только ошибки
    assert any(line.startswith("  ошибка: 17-03-2027 19:00 uk -> Українка @twin_b — ") for line in lines)


class _LoginPhase:
    """Фаза входов для runner: вход — через describe_channel(allow_login=True), как у ChannelBook."""

    def __init__(self, platform: FakePlatform) -> None:
        self._platform: FakePlatform = platform
        self.phases: list[tuple[list[str], int]] = []   # (каналы, сколько list_upcoming было до фазы)

    def log_in_needed(self, channels: Sequence[ChannelConfig]) -> None:
        keys: list[str] = list(dict.fromkeys(channel.key for channel in channels))
        self.phases.append((keys, len(self._platform.list_calls)))
        for channel in {channel.key: channel for channel in channels}.values():
            if channel.key in self._platform.tokens_missing:
                self._platform.drop_login(channel)
                self._platform.describe_channel(channel, allow_login=True)
                self._platform.keep_login(channel)

    def channel(self, config: ChannelConfig) -> Channel:
        return _channel_object(config, ChannelStatus.READY)


def _channel_object(config: ChannelConfig, status: ChannelStatus, error: PlatformError | None = None) -> Channel:
    channel: Channel = Channel(config=config, token_file=Path(f"{config.handle}.token.json"), status=status)
    channel.error = error
    return channel


def test_logins_happen_before_any_channel_is_listed(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Все входы — подряд до первого list_upcoming; в сверке и действиях браузер не открывается."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    fake_platform.tokens_missing = {"yt_ua", "yt_ru"}
    logins: _LoginPhase = _LoginPhase(fake_platform)
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(), planer_paths, fake_platform, FakeFormSender(), now, rng, logins=logins
    )
    assert logins.phases == [(["yt_ru", "yt_ua"], 0)]
    assert fake_platform.logins == ["yt_ru", "yt_ua"]
    assert fake_platform.list_calls == ["yt_ru", "yt_ua"]
    assert outcome.exit_code == ExitCode.OK and len(fake_platform.created) == 2


def test_channel_without_login_after_the_phase_is_an_error_not_a_browser(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Канал, который в фазе входов не вошёл, — ошибка его объектов; другие каналы обработаны."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    fake_platform.tokens_missing = {"yt_ua"}
    fake_platform.fail_login["yt_ua"] = PlatformError("authFailed", "flow_failed: browser closed")

    failure: PlatformError = PlatformError("authFailed", "flow_failed: browser closed")

    class _FailingPhase:
        def log_in_needed(self, channels: Sequence[ChannelConfig]) -> None:
            for channel in channels:
                if channel.key in fake_platform.tokens_missing:
                    fake_platform.drop_login(channel)
                    with pytest.raises(PlatformError):
                        fake_platform.describe_channel(channel, allow_login=True)

        def channel(self, config: ChannelConfig) -> Channel:
            if config.key in fake_platform.tokens_missing:
                return _channel_object(config, ChannelStatus.FAILED, failure)
            return _channel_object(config, ChannelStatus.READY)

    outcome: RunOutcome = run(
        RunMode.FULL, make_config(), planer_paths, fake_platform, FakeFormSender(), now, rng, logins=_FailingPhase()
    )
    assert fake_platform.logins == ["yt_ua"]                      # один вход — в фазе, не в сверке
    assert fake_platform.list_calls == ["yt_ru"]                  # к каналу без входа площадка не спрашивается
    assert outcome.report is not None and outcome.exit_code == ExitCode.ERRORS
    errors = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.ERROR]
    assert [(item.account_name, item.date, item.error.code if item.error else None) for item in errors] == [
        ("yt_ua", None, "authFailed")                             # полный текст — один раз на канал
    ]
    not_admitted = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.NOT_ADMITTED]
    assert [(item.account_name, item.admission_texts) for item in not_admitted] == [
        ("yt_ua", (msg.ADMISSION_CHANNEL_TEXT["failed"],))
    ]
    assert [call.channel_id for call in fake_platform.created] == ["yt_ru"]


def test_forms_are_read_before_the_platform_is_touched(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk")],
    )
    sender: FakeFormSender = FakeFormSender(platform=fake_platform)
    run(RunMode.DRY_RUN, make_config(), planer_paths, fake_platform, sender, now, rng)
    assert sender.prepared == [((FORM_SPEC["url"],), 0)]
    assert fake_platform.list_calls == ["yt_ua"]


def test_status_logs_in_every_channel_first(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    sender: FakeFormSender = FakeFormSender()
    logins: _LoginPhase = _LoginPhase(fake_platform)
    run(RunMode.STATUS, make_config(), planer_paths, fake_platform, sender, now, rng, logins=logins)
    assert logins.phases == [(["yt_ua", "yt_ru"], 0)]
    assert sender.prepared == []                                   # в --status форм нет


# --- допуск к публикации и ключ сразу после действий (живой прогон 17-09-2026 01:08)

NICK_KEY: str = "nick"
NICK_CONFIG_CHANNELS: tuple[tuple[str, list[str]], ...] = ((NICK_KEY, ["en"]),)


def _form_with_dates(tmp_path: Path, *dates: str) -> KeyForm:
    """Тренировочная форма в разметке FB_PUBLIC_LOAD_DATA_, в «Время стрима» — только эти даты."""
    items: list[Any] = default_items()
    items[0][4][0][1] = [["Украинский ( Ukranian)"], ["Русский ( Russian)"], ["Английский ( English)"]]
    items[2][4][0][1] = [[f"{date} Дата стрима (время стрима указано в объявлении)"] for date in dates]
    session: _FakeSession = _FakeSession(_FakeResponse(build_html(build_payload(items))))
    structure: FormStructure = FormDiscovery(session, tmp_path / "form", datetime(2027, 3, 16, 12, 0)).structure(
        FORM_SPEC["url"]
    )
    return KeyForm.build(build_form_spec(), structure)


def _nick_slots(make_slot: SlotFactory) -> list[dict[str, Any]]:
    return [
        make_slot("17-03-2027", "19:00", "en"),
        make_slot("18-03-2027", "20:00", "en"),
        make_slot("17-03-2027", "21:00", "en"),
    ]


def test_slot_without_date_in_form_is_not_published_and_keys_go_right_after_actions(
    tmp_path: Path,
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    make_package(planer_paths.bcast_dir, slots=_nick_slots(make_slot))
    sender: FakeFormSender = FakeFormSender(platform=fake_platform, key_form=_form_with_dates(tmp_path, "17.03.2027"))
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(NICK_CONFIG_CHANNELS), planer_paths, fake_platform, sender, now, rng
    )
    assert [call.marker for call in fake_platform.created] == ["17-03-2027_1900_en", "17-03-2027_2100_en"]
    assert [call.slot_id for call in sender.calls] == ["17-03-2027_1900_en", "17-03-2027_2100_en"]
    assert sender.sent_after_created == [1, 2]      # ключ каждого — до действий по следующему объекту
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None
    [blocked] = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.NOT_ADMITTED]
    assert (blocked.date, blocked.time, blocked.form) == ("18-03-2027", "20:00", None)
    text: str = _report_text(outcome)
    assert "## Не допущено к публикации (1)" in text
    assert (
        "- 18-03-2027 20:00 en -> nick @nick — в форме нет варианта «Время стрима ( Stream time ): 18.03.2027» "
        "— нужен владельцу формы; эфира на канале нет"
    ) in text.splitlines()
    assert text.index("## Не допущено к публикации") < text.index("## Создано")
    assert "## Ошибки" not in text
    assert "18-03-2027" not in planer_paths.keys_file.read_text(encoding="utf-8")   # эфира нет — и ключа нет


def test_existing_broadcast_of_a_not_admitted_slot_keeps_its_key_out_of_the_form(
    tmp_path: Path,
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Запуск 01:18: эфир 18-03 уже стоит — ключ в keys.txt с причиной, в форму ничего, ничего не правится."""
    make_package(planer_paths.bcast_dir, slots=_nick_slots(make_slot))
    fake_platform.seed_broadcast(
        NICK_KEY, datetime.fromisoformat("2027-03-18T20:00:00+02:00"), "Другое название", "",
        marker="18-03-2027_2000_en", stream_key=PLATFORM_KEY,
    )
    sender: FakeFormSender = FakeFormSender(platform=fake_platform, key_form=_form_with_dates(tmp_path, "17.03.2027"))
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(NICK_CONFIG_CHANNELS), planer_paths, fake_platform, sender, now, rng
    )
    assert "18-03-2027_2000_en" not in [call.slot_id for call in sender.calls]
    assert fake_platform.updated == [] and "fakebc00001" not in fake_platform.settings_calls
    assert "fakebc00001" not in fake_platform.facts_calls
    keys: str = planer_paths.keys_file.read_text(encoding="utf-8")
    block: str = keys.split("18-03-2027 20:00  en  nick @nick\n", 1)[1].split("\n\n", 1)[0]
    assert f"  ключ   {PLATFORM_KEY}" in block
    assert (
        "  форма  НЕ отправлен: не допущено — в форме нет варианта «Время стрима ( Stream time ): 18.03.2027» "
        "— нужен владельцу формы"
    ) in block
    assert "эфир на канале: https://www.youtube.com/watch?v=fakebc00001" in _report_text(outcome)
    assert outcome.report is not None
    console: str = render_console(outcome.report, root=planer_paths.root)
    assert (
        "  не допущено: 18-03-2027 20:00 en -> nick @nick — в форме нет варианта «Время стрима ( Stream time ): "
        "18.03.2027» — нужен владельцу формы; эфир на канале есть — ключ стримеру не передан"
    ) in console.splitlines()
    assert "не допущено 1" in console.splitlines()[0]


def test_failed_send_of_one_object_does_not_stop_the_next(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "en"), make_slot("17-03-2027", "21:00", "en")],
    )
    sender: _PartialFormSender = _PartialFormSender("17-03-2027_2100_en")
    outcome: RunOutcome = run(
        RunMode.FULL, make_config(NICK_CONFIG_CHANNELS), planer_paths, fake_platform, sender, now, rng
    )
    assert len(fake_platform.created) == 2
    assert sender.calls == ["17-03-2027_1900_en", "17-03-2027_2100_en"]
    assert outcome.exit_code == ExitCode.ERRORS                 # первый ключ не дошёл
    keys: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "форма  НЕ отправлен — форма не подтвердила" in keys and "форма  отправлен в форму" in keys


def test_incomplete_answers_after_publication_skip_the_post(
    tmp_path: Path,
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Адреса потока нет среди вариантов формы: эфир создан, POST нет, причина — в keys.txt."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "en")])
    form: KeyForm = _form_with_dates(tmp_path, "17.03.2027")
    url_question = form.questions["stream_url"]
    assert url_question is not None
    other_urls = replace(url_question, options=("rtmp://x.rtmp.youtube.com/live2/",))
    form = replace(form, questions={**form.questions, "stream_url": other_urls})
    sender: FakeFormSender = FakeFormSender(key_form=form)
    with caplog.at_level("WARNING"):
        outcome: RunOutcome = run(
            RunMode.FULL, make_config(NICK_CONFIG_CHANNELS), planer_paths, fake_platform, sender, now, rng
        )
    assert len(fake_platform.created) == 1 and sender.calls == []
    assert any(message.startswith("form_send_skipped slot_id=17-03-2027_1900_en") for message in caplog.messages)
    assert outcome.exit_code == ExitCode.ERRORS
    assert "НЕ отправлен — в форме нет нужного варианта ответа" in planer_paths.keys_file.read_text(encoding="utf-8")


def test_dry_run_shows_admission_and_sends_nothing(
    tmp_path: Path,
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    make_package(planer_paths.bcast_dir, slots=_nick_slots(make_slot))
    sender: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027"))
    with caplog.at_level("INFO"):
        outcome: RunOutcome = run(
            RunMode.DRY_RUN, make_config(NICK_CONFIG_CHANNELS), planer_paths, fake_platform, sender, now, rng
        )
    assert sender.calls == [] and fake_platform.created == []
    assert outcome.report is not None and outcome.exit_code == ExitCode.ERRORS
    kinds = [item.kind for item in outcome.report.outcomes]
    assert kinds.count(OutcomeKind.NOT_ADMITTED) == 1 and kinds.count(OutcomeKind.CREATED) == 2
    assert "Итог: опубликуем 2, исправим 0, уже стояло 0, не допущено 1," in _report_text(outcome)
    assert "slot_not_admitted slot_id=18-03-2027_2000_en" in "\n".join(caplog.messages)
    assert any(
        message.startswith(
            'slot_not_admitted_reason slot_id=18-03-2027_2000_en channel="nick" handle=@nick '
            "kind=form_field code=missingOption field=date"
        )
        for message in caplog.messages
    )
    assert sum(1 for message in caplog.messages if message.startswith("slot_admitted ")) == 2


# --- задача 5m-C: память планера и одно правило отправки ключа

UK_KEY: str = "yt_ua"                     # без ChannelBook ключ записи — ключ канала (тесты)


class _CrashingSender(FakeFormSender):
    """Обрыв посреди отправки: исключение до подтверждения формы."""

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        super().send(planned)
        raise RuntimeError("обрыв до подтверждения")


def _open_store(paths: PlanerPaths, *, read_only: bool = False) -> RecordStore:
    return RecordStore.open(paths.records_file, read_only=read_only, now_local=FIXED_NOW)


def _run_with_memory(
    mode: RunMode,
    paths: PlanerPaths,
    config: PlanerConfig,
    platform: FakePlatform,
    sender: FakeFormSender,
    now: datetime,
    rng: random.Random,
    clock: Callable[[], datetime] | None = None,
) -> RunOutcome:
    """Как main: память открыта на запуск (в --dry-run и --status — только чтение) и закрыта после него.

    Без clock часы стоят на now.
    """
    store: RecordStore = _open_store(paths, read_only=mode is not RunMode.FULL)
    try:
        return run(mode, config, paths, platform, sender, now, rng, store=store, clock=clock or (lambda: now))
    finally:
        store.close()


def _stored(paths: PlanerPaths, slot_id: str) -> SlotRecord | None:
    store: RecordStore = _open_store(paths, read_only=True)
    try:
        return store.find(slot_id, UK_KEY)
    finally:
        store.close()


def _uk_package(make_package: PackageFactory, make_slot: SlotFactory, paths: PlanerPaths, **kwargs: Any) -> Path:
    return make_package(paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk", **kwargs)])


def test_interrupted_send_is_finished_by_the_next_run(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """а) эфир создан, запуск оборвался до подтверждения формы — следующий запуск отправляет ключ ровно раз."""
    _uk_package(make_package, make_slot, planer_paths)
    form: KeyForm = _form_with_dates(tmp_path, "17.03.2027")
    crashing: _CrashingSender = _CrashingSender(key_form=form)
    with pytest.raises(RuntimeError):
        _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, crashing, now, rng)
    after_crash: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert after_crash is not None and after_crash.stage is SlotStage.PUBLISHED
    assert after_crash.results.stream_key == "fake-0001-0000-0000-0000"
    sender: FakeFormSender = FakeFormSender(key_form=form)
    outcome: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert [call.stream_key for call in sender.calls] == ["fake-0001-0000-0000-0000"]
    assert outcome.report.outcomes[0].form is FormState.SENT
    assert "; ключ передан в форму" in _report_text(outcome)
    confirmed: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert confirmed is not None and confirmed.stage is SlotStage.KEY_CONFIRMED
    assert len(fake_platform.created) == 1
    third: FakeFormSender = FakeFormSender(key_form=form)
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, third, now, rng)
    assert third.calls == []


def test_confirmed_match_sends_nothing(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """б) подтверждённый MATCH — ни одного POST."""
    _uk_package(make_package, make_slot, planer_paths)
    sender: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027"))
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    second: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert len(sender.calls) == 1 and second.exit_code == ExitCode.OK
    assert "передан в форму 16-03-2027" in planer_paths.keys_file.read_text(encoding="utf-8")
    assert msg.WARNING_KEPT_KEY in _report_text(second)


def test_update_sends_only_when_answers_change(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """в) исправлен с теми же ответами — POST нет; изменилось название канала в ответах — POST."""
    make_package(planer_paths.bcast_dir, generated_at="13-09-2026 10:15", slots=[make_slot("17-03-2027", "19:00", "uk")])
    sender: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027"))
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    make_package(
        planer_paths.bcast_dir, generated_at="14-09-2026 10:15",
        slots=[make_slot("17-03-2027", "19:00", "uk", title="Новое название эфира")],
    )
    second: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.FIXED
    assert len(fake_platform.updated) == 1 and len(sender.calls) == 1
    make_package(
        planer_paths.bcast_dir, generated_at="15-09-2026 10:15",
        slots=[make_slot("17-03-2027", "19:00", "uk", title="Ещё одно название")],
    )
    base: PlanerConfig = make_config()
    renamed: PlanerConfig = replace(
        base, channels=(replace(base.channels[0], account_name="Новое имя канала"), *base.channels[1:])
    )
    third: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, renamed, fake_platform, sender, now, rng)
    assert third.report is not None and third.report.outcomes[0].kind is OutcomeKind.FIXED
    assert [call.account_name for call in sender.calls] == ["yt_ua", "Новое имя канала"]
    assert {call.stream_key for call in sender.calls} == {"fake-0001-0000-0000-0000"}


def test_broadcast_removed_by_hand_is_recreated_and_its_new_key_sent(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random,
) -> None:
    """г) эфир удалён на площадке между запусками — создан новый, новый ключ отправлен."""
    _uk_package(make_package, make_slot, planer_paths)
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    fake_platform.remove_broadcast(UK_KEY, fake_platform.created[0].broadcast_id)
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [call.stream_key for call in form_sender.calls] == ["fake-0001-0000-0000-0000", "fake-0002-0000-0000-0000"]
    record: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert record is not None and record.results.confirmed_stream_key == "fake-0002-0000-0000-0000"


def test_first_run_with_memory_bootstraps_marked_broadcasts(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random,
) -> None:
    """д) базы нет: эфир с меткой — «передан до памяти», POST нет; ручной (усыновление) и новый — POST."""
    marked, manual, new = (
        make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk"), make_slot("19-03-2027", "19:00", "uk")
    )
    make_package(planer_paths.bcast_dir, slots=[marked, manual, new])
    fake_platform.seed_broadcast(
        UK_KEY, UK_START, marked["title"], marked["description"], marker=UK_SLOT, stream_key=PLATFORM_KEY
    )
    fake_platform.seed_broadcast(
        UK_KEY, datetime.fromisoformat("2027-03-18T19:00:00+02:00"), manual["title"], manual["description"],
        marker="ручной ключ", stream_key="mmmm-mmmm-mmmm-mmmm-mmmm",
    )
    assert not planer_paths.records_file.exists()
    outcome: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert sorted(call.slot_id for call in form_sender.calls) == ["18-03-2027_1900_uk", "19-03-2027_1900_uk"]
    record: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert record is not None and record.stage is SlotStage.KEY_CONFIRMED and record.results.is_bootstrap
    keys: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert keys.split(f"{PLATFORM_KEY}\n", 1)[1].split("\n\n", 1)[0].endswith(msg.KEY_FORM_BOOTSTRAP)
    created_line: str = msg.WARNING_RECORDS_CREATED.format(path=planer_paths.records_file, count=1)
    assert outcome.report is not None and created_line in outcome.report.run_warnings
    assert f"  {created_line}" in render_console(outcome.report, root=planer_paths.root).splitlines()
    second: FakeFormSender = FakeFormSender()
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, second, now, rng)
    assert second.calls == []                          # второй запуск подряд — ни одного POST


def test_error_after_the_key_was_taken_does_not_stop_the_send(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """е) превью пропало из пакета после создания; сбой update после привязки потока — ключ всё равно ушёл."""
    import app.pipeline.runner as runner_module

    def _missing_preview(package: Any, name: str) -> bytes:
        if "17-03-2027" in name:
            raise PackageError(PackageErrorReason.PREVIEW_MISSING, name)
        return b"x"

    monkeypatch.setattr(runner_module, "read_preview", _missing_preview)
    uk: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    ru: dict[str, Any] = make_slot("18-03-2027", "19:00", "ru")
    make_package(planer_paths.bcast_dir, slots=[uk, ru])
    bare: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ru", datetime.fromisoformat("2027-03-18T19:00:00+02:00"), "Старое название", ru["description"]
    )
    fake_platform.fail_update[bare.broadcast_id] = PlatformError("backendError", "HTTP 503")
    outcome: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    kinds = {item.date: item.kind for item in outcome.report.outcomes if item.date}
    assert kinds == {"17-03-2027": OutcomeKind.ERROR, "18-03-2027": OutcomeKind.ERROR}
    assert sorted(call.slot_id for call in form_sender.calls) == [UK_SLOT, "18-03-2027_1900_ru"]
    assert len(fake_platform.created) == 1 and len(fake_platform.attached) == 1


def test_slot_admitted_later_sends_the_key_of_its_standing_broadcast(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """ж) 17-09: 18-03 не допущен (нет даты) → владелец формы добавил дату → MATCH без подтверждения → ключ ушёл."""
    make_package(planer_paths.bcast_dir, slots=_nick_slots(make_slot))
    fake_platform.seed_broadcast(
        NICK_KEY, datetime.fromisoformat("2027-03-18T20:00:00+02:00"), "Эфир 18-03-2027_2000_en", "Описание эфира",
        marker="18-03-2027_2000_en", stream_key=PLATFORM_KEY,
    )
    config: PlanerConfig = make_config(NICK_CONFIG_CHANNELS)
    _run_with_memory(RunMode.FULL, planer_paths, config, fake_platform, FakeFormSender(), now, rng)   # память создана
    only_17: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027"))
    first: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, config, fake_platform, only_17, now, rng)
    assert first.exit_code == ExitCode.ERRORS and "18-03-2027_2000_en" not in [call.slot_id for call in only_17.calls]
    both: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027", "18.03.2027"))
    second: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, config, fake_platform, both, now, rng)
    assert [call.stream_key for call in both.calls if call.slot_id == "18-03-2027_2000_en"] == [PLATFORM_KEY]
    assert second.report is not None
    [nick] = [item for item in second.report.outcomes if item.date == "18-03-2027"]
    assert (nick.kind, nick.form) == (OutcomeKind.MATCHED, FormState.SENT)


def test_new_form_address_sends_standing_keys_there(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random,
) -> None:
    """з) адрес формы сменился (переход на боевую) — ключи стоящих эфиров уходят в новую форму."""
    battle_url: str = "https://forms.gle/BattleFormCCC"
    make_package(planer_paths.bcast_dir, generated_at="13-09-2026 10:15", slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    make_package(
        planer_paths.bcast_dir, generated_at="14-09-2026 10:15", form={**FORM_SPEC, "url": battle_url},
        slots=[make_slot("17-03-2027", "19:00", "uk")],
    )
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [(call.form_url, call.stream_key) for call in form_sender.calls] == [
        (FORM_SPEC["url"], "fake-0001-0000-0000-0000"), (battle_url, "fake-0001-0000-0000-0000")
    ]


def test_dry_run_reads_memory_and_writes_nothing(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """и) dry-run: базы нет — не создаётся; есть — не меняется; «ключ будет передан» — по записям."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk")])
    # без обложек: две одинаковые свои обложки одного канала читались бы как заглушка (правило дублей)
    config: PlanerConfig = make_config(set_thumbnail=False)
    _run_with_memory(RunMode.DRY_RUN, planer_paths, config, fake_platform, FakeFormSender(), now, rng)
    assert not planer_paths.records_file.exists()
    failing: _PartialFormSender = _PartialFormSender("17-03-2027_1900_uk")      # 18-03 форма не подтвердила
    _run_with_memory(RunMode.FULL, planer_paths, config, fake_platform, failing, now, rng)
    before: bytes = planer_paths.records_file.read_bytes()
    sender: FakeFormSender = FakeFormSender()
    outcome: RunOutcome = _run_with_memory(RunMode.DRY_RUN, planer_paths, config, fake_platform, sender, now, rng)
    assert planer_paths.records_file.read_bytes() == before and sender.calls == []
    assert outcome.report is not None
    forms = {item.date: item.form for item in outcome.report.outcomes}
    assert forms == {"17-03-2027": None, "18-03-2027": FormState.PLANNED}
    assert f"- 18-03-2027 19:00 uk -> yt_ua @yt_ua — https://www.youtube.com/watch?v=fakebc00002; {msg.FORM_MARK_PLANNED}{msg.OUTCOME_DRY_RUN_SUFFIX}" in (
        _report_text(outcome).splitlines()
    )


def test_old_records_are_cleaned_by_keep_days(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random, caplog: pytest.LogCaptureFixture,
) -> None:
    """к) записи слотов старше keep_days удаляются в полном запуске."""
    store: RecordStore = _open_store(planer_paths)
    for slot_id, start in (("01-01-2027_1900_uk", "2027-01-01T17:00:00+00:00"), ("10-03-2027_1900_uk", "2027-03-10T17:00:00+00:00")):
        store.save(SlotRecord(slot_id, UK_KEY, start, SlotStage.KEY_CONFIRMED, "01-01-2027 12:00", RecordResults()))
    store.close()
    _uk_package(make_package, make_slot, planer_paths)
    with caplog.at_level("INFO"):
        _run_with_memory(RunMode.FULL, planer_paths, make_config(keep_days=30), fake_platform, form_sender, now, rng)
    assert _stored(planer_paths, "01-01-2027_1900_uk") is None
    assert _stored(planer_paths, "10-03-2027_1900_uk") is not None
    assert _stored(planer_paths, UK_SLOT) is not None
    assert "records_cleaned removed=1" in caplog.messages


def test_record_log_lines_follow_the_object(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime,
    rng: random.Random, caplog: pytest.LogCaptureFixture,
) -> None:
    _uk_package(make_package, make_slot, planer_paths)
    with caplog.at_level("INFO"):
        _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    names: list[str] = [
        message.split(" ", 1)[0] + (" " + message.rsplit("stage=", 1)[1] if message.startswith("record_saved") else "")
        for message in caplog.messages
        if message.startswith(("broadcast_created", "record_saved", "form_send "))
    ]
    assert names == [
        "record_saved admitted", "broadcast_created", "record_saved published", "form_send", "record_saved key_confirmed"
    ]


class _SteppingClock:
    """Часы запуска: стоят, пока их не сдвинут; отправитель формы сдвигает их в момент отправки."""

    def __init__(self, start: datetime) -> None:
        self.now: datetime = start

    def __call__(self) -> datetime:
        return self.now


class _ClockMovingSender(FakeFormSender):
    """Форма отвечает не сразу: к подтверждению часы ушли вперёд."""

    def __init__(self, clock: _SteppingClock, step: timedelta, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._clock: _SteppingClock = clock
        self._step: timedelta = step

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        self._clock.now = self._clock.now + self._step
        return super().send(planned)


def _clock_text(moment: datetime) -> str:
    return format_datetime_text(moment.astimezone())


def test_event_times_come_from_the_clock_at_the_event(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """Публикация и подтверждение — моменты событий, а не время старта запуска."""
    _uk_package(make_package, make_slot, planer_paths)
    published: datetime = now + timedelta(minutes=11)
    confirmed: datetime = published + timedelta(minutes=3)
    clock: _SteppingClock = _SteppingClock(published)
    sender: _ClockMovingSender = _ClockMovingSender(
        clock, confirmed - published, key_form=_form_with_dates(tmp_path, "17.03.2027")
    )
    outcome: RunOutcome = _run_with_memory(
        RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng, clock=clock
    )
    assert outcome.exit_code == ExitCode.OK and len(sender.calls) == 1
    record: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert record is not None
    assert record.results.published_at == _clock_text(published)
    assert record.results.confirmed_at == _clock_text(confirmed)
    assert record.updated_at == _clock_text(confirmed)
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert f"отправлен в форму {_clock_text(confirmed)}" in keys_text
    assert outcome.report is not None and _clock_text(now) == outcome.report.generated_at_text
    # следующий запуск показывает момент подтверждения из памяти
    later: datetime = confirmed + timedelta(hours=2)
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, later, rng)
    assert f"передан в форму {_clock_text(confirmed)}" in planer_paths.keys_file.read_text(encoding="utf-8")


def _record_lines(caplog: pytest.LogCaptureFixture, name: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.getMessage().startswith(name + " ")]


def test_unchanged_record_is_not_written_again(
    tmp_path: Path, planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory,
    make_config: ConfigFactory, fake_platform: FakePlatform, now: datetime, rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Запуск без изменений ничего не пишет в память; изменение — пишет; в лог — итоговая и запрошенная стадии."""
    caplog.set_level(logging.DEBUG, logger="planer")
    _uk_package(make_package, make_slot, planer_paths)
    sender: FakeFormSender = FakeFormSender(key_form=_form_with_dates(tmp_path, "17.03.2027"))
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    # второй запуск: решение сменилось (создан -> совпал) — снимок записи другой, одна запись
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    before: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    caplog.clear()
    later: datetime = now + timedelta(hours=1)
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, later, rng)
    assert _record_lines(caplog, "record_saved") == []
    unchanged: list[str] = _record_lines(caplog, "record_unchanged")
    assert unchanged and all("stage=key_confirmed" in line for line in unchanged)
    assert _stored(planer_paths, UK_SLOT) == before          # updated_at тоже прежний: записи не было
    assert len(sender.calls) == 1
    caplog.clear()
    make_package(
        planer_paths.bcast_dir, generated_at="14-09-2026 10:15",
        slots=[make_slot("17-03-2027", "19:00", "uk", title="Новое название эфира")],
    )
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, later, rng)
    saved: list[str] = _record_lines(caplog, "record_saved")
    assert saved and saved[0].endswith("stage=key_confirmed requested=admitted")
    after: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert after is not None and after.updated_at == _clock_text(later)


# --- 5m-E: итог исправления по факту, отказ обложек, обложка в памяти, эфиры до памяти, прошедшие слоты

LIMIT_ERROR: PlatformError = PlatformError("uploadRateLimitExceeded", "HTTP 429: limit")


def _slot_start(spec: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(spec["start"])


def _seed_spec(fake_platform: FakePlatform, spec: dict[str, Any], key: str, **overrides: Any) -> UpcomingBroadcast:
    """Эфир планера на yt_ua по слоту spec; по умолчанию картинка — заглушка канала (обложки нет)."""
    placeholder: str = FakePlatform.placeholder_of("yt_ua")
    values: dict[str, Any] = {
        "marker": spec["slot_id"],
        "stream_key": key,
        "picture": placeholder,
        "stream_description": PLACEHOLDER_TOKEN.format(sha=placeholder),
    }
    values.update(overrides)
    title: str = values.pop("title", spec["title"])
    return fake_platform.seed_broadcast("yt_ua", _slot_start(spec), title, spec["description"], **values)


def test_thumbnail_refused_on_fix_is_matched_not_fixed(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """а) Обложка не поставилась (429): эфир «уже стоял» с хвостом «обложка не поставлена», ИСПРАВИЛИ пуст."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    found: UpcomingBroadcast = _seed_spec(fake_platform, spec, PLATFORM_KEY)
    fake_platform.fail_thumbnail[found.broadcast_id] = LIMIT_ERROR
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
    assert outcome.report is not None
    [pair] = outcome.report.outcomes
    assert (pair.kind, pair.changed_fields, pair.unfixed_fields) == (OutcomeKind.MATCHED, (), ("thumbnail",))
    assert pair.field_changes == ()
    console: str = render_console(outcome.report, root=planer_paths.root)
    assert msg.CONSOLE_BLOCK_FIXED not in console
    assert msg.UNFIXED_FIELD_TEXT["thumbnail"] in console
    report_text: str = _report_text(outcome)
    assert "исправлено, ключ и ссылка прежние" not in report_text
    assert msg.OUTCOME_UNFIXED.format(what=msg.UNFIXED_FIELD_TEXT["thumbnail"]) in report_text
    assert any(msg.THUMBNAIL_REASON_TEXT["uploadRateLimitExceeded"] in line for line in outcome.report.warnings)


def test_refused_thumbnails_skip_empty_resends(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """б) После отказа обложек: только обложка — ни update, ни загрузки; ещё и название — update без загрузки."""
    caplog.set_level(logging.INFO, logger="planer")
    first, second, third = (make_slot(date, "19:00", "uk") for date in ("17-03-2027", "18-03-2027", "19-03-2027"))
    make_package(planer_paths.bcast_dir, slots=[first, second, third])
    refused: UpcomingBroadcast = _seed_spec(fake_platform, first, "aaaa-aaaa-aaaa-aaaa-aaaa")
    only_cover: UpcomingBroadcast = _seed_spec(fake_platform, second, "bbbb-bbbb-bbbb-bbbb-bbbb")
    with_title: UpcomingBroadcast = _seed_spec(
        fake_platform, third, "cccc-cccc-cccc-cccc-cccc", title="Старое название"
    )
    fake_platform.fail_thumbnail[refused.broadcast_id] = LIMIT_ERROR
    outcome: RunOutcome = _run(
        RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng, _memory_without_confirmations()
    )
    assert [call.broadcast_id for call in fake_platform.updated] == [refused.broadcast_id, with_title.broadcast_id]
    assert fake_platform.thumbnail_attempts == [refused.broadcast_id]
    skipped: list[str] = _record_lines(caplog, "broadcast_fix_skipped")
    assert len(skipped) == 1
    assert only_cover.broadcast_id in skipped[0] and "reason=uploadRateLimitExceeded" in skipped[0]
    assert outcome.report is not None
    kinds: dict[str | None, tuple[OutcomeKind, tuple[str, ...], tuple[str, ...]]] = {
        pair.date: (pair.kind, pair.changed_fields, pair.unfixed_fields) for pair in outcome.report.outcomes
    }
    assert kinds["18-03-2027"] == (OutcomeKind.MATCHED, (), ("thumbnail",))
    assert kinds["19-03-2027"] == (OutcomeKind.FIXED, ("title",), ("thumbnail",))
    console: str = render_console(outcome.report, root=planer_paths.root)
    assert f"обновлено: название; {msg.UNFIXED_FIELD_TEXT['thumbnail']}" in console
    assert "обновлено: название, обложка" not in console


def test_thumbnail_set_by_planer_is_remembered_over_a_stale_picture(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """в) Картинка на площадке ещё заглушка, но обложку этому эфиру ставил планер — MATCH, повторной загрузки нет."""
    caplog.set_level(logging.INFO, logger="planer")
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[spec])
    fake_platform.picture_lags = True
    _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    record: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert record is not None and record.results.thumbnail_broadcast_id == created.broadcast_id
    assert record.results.thumbnail_set_at == format_datetime_text(now.astimezone())
    second: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert fake_platform.updated == [] and len(fake_platform.thumbnail_attempts) == 1
    assert any(created.broadcast_id in line for line in _record_lines(caplog, "thumbnail_from_memory"))
    # владелец удалил эфир и завёл новый с той же меткой: память — про другой эфир, сверка по картинке
    fake_platform.remove_broadcast("yt_ua", created.broadcast_id)
    renewed: UpcomingBroadcast = _seed_spec(fake_platform, spec, PLATFORM_KEY)
    third: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert third.report is not None
    assert [call.broadcast_id for call in fake_platform.updated] == [renewed.broadcast_id]
    assert fake_platform.thumbnail_attempts[-1] == renewed.broadcast_id
    stored: SlotRecord | None = _stored(planer_paths, UK_SLOT)
    assert stored is not None and stored.results.thumbnail_broadcast_id == renewed.broadcast_id


def test_broadcasts_older_than_memory_are_bootstrapped_in_any_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """г) Память уже была; эфир с меткой старше памяти — подтверждение без POST; эфир моложе памяти — POST."""
    _open_store(planer_paths).close()               # память создана прошлым запуском, created_utc = FIXED_NOW
    older: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    newer: dict[str, Any] = make_slot("18-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[older, newer])
    _seed_spec(
        fake_platform, older, "aaaa-aaaa-aaaa-aaaa-aaaa", picture="aaaaaaaaaaaa",
        published_utc=FIXED_NOW - timedelta(days=1),
    )
    _seed_spec(
        fake_platform, newer, "bbbb-bbbb-bbbb-bbbb-bbbb", picture="aaaaaaaaaaaa",
        published_utc=FIXED_NOW + timedelta(hours=1),
    )
    outcome: RunOutcome = _run_with_memory(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [call.stream_key for call in form_sender.calls] == ["bbbb-bbbb-bbbb-bbbb-bbbb"]
    bootstrapped: SlotRecord | None = _stored(planer_paths, older["slot_id"])
    assert bootstrapped is not None and bootstrapped.results.is_bootstrap
    assert outcome.report is not None
    assert msg.WARNING_RECORDS_CREATED.format(count=1) in outcome.report.warnings
    assert msg.KEY_FORM_BOOTSTRAP in planer_paths.keys_file.read_text(encoding="utf-8")


def test_broadcast_of_a_past_slot_is_not_an_orphan(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """е) Слот уже прошёл, эфир ещё не начался: «пропущено — уже прошло», но не «перенесён или отменён»."""
    past: dict[str, Any] = make_slot("16-03-2027", "11:00", "uk")
    future: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.bcast_dir, slots=[past, future])
    _seed_spec(fake_platform, past, "aaaa-aaaa-aaaa-aaaa-aaaa")
    _seed_spec(fake_platform, make_slot("16-03-2027", "09:00", "uk"), "bbbb-bbbb-bbbb-bbbb-bbbb")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert [(orphan.date, orphan.time) for orphan in outcome.report.orphans] == [("16-03-2027", "09:00")]
    assert any(line.kind is SkipKind.PAST for line in outcome.report.skipped)
