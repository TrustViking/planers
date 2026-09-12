from __future__ import annotations

import random
import re

import pytest
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import FormState, OutcomeKind, PackageLineStatus
from app.paths import PlanerPaths
from app.pipeline.plan import Decision, PlannedBroadcast
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.form.base import FORM_CODE_NOT_CONFIRMED, FormSendResult
from app.platforms.base import BroadcastFacts, PlatformError, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.state.registry import FormStatus, Registration, Registry
from app.tests.conftest import FORM_SPEC, FakeFormSender
from app.ui import messages_ru as msg

PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]
ConfigFactory = Callable[..., PlanerConfig]
UK_SLOT: str = "17-03-2027_1900_uk"
UK_KEY: str = f"{UK_SLOT}|yt_ua"
UK_START: datetime = datetime.fromisoformat("2027-03-17T19:00:00+02:00")
PLATFORM_KEY: str = "abcd-abcd-abcd-abcd-abcd"
JOURNAL_KEY: str = "oldk-oldk-oldk-oldk-oldk"


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


def _registration(**overrides: Any) -> Registration:
    values: dict[str, Any] = dict(
        slot_id=UK_SLOT,
        channel_id="yt_ua",
        account_name="Account yt_ua",
        language="uk",
        date="17-03-2027",
        time="19:00",
        broadcast_id="oldbc",
        broadcast_url="https://www.youtube.com/watch?v=oldbc",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="oldk-oldk-oldk-oldk-oldk",
        package_id="pkg-old",
        created_at=datetime(2027, 3, 10, 9, 0),
        form_status=FormStatus.SENT,
        form_sent_at=datetime(2027, 3, 10, 9, 0),
        previous_broadcast_ids=[],
        last_error=None,
    )
    values.update(overrides)
    return Registration(**values)


def _save_registry(paths: PlanerPaths, *registrations: Registration) -> None:
    registry: Registry = Registry()
    for registration in registrations:
        registry.upsert(registration)
    registry.save(paths.registry_file)


def _loaded(paths: PlanerPaths, key: str) -> Registration | None:
    return Registry.load(paths.registry_file).get(key)


def _kept_key_lines(outcome: RunOutcome) -> list[str]:
    assert outcome.report is not None
    return [line for line in outcome.report.warnings if line == msg.WARNING_KEPT_KEY]


def test_full_create_registers_and_confirms_form(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=2)])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert registration.form_status is FormStatus.SENT
    assert registration.stream_key == "fake-0001-0000-0000-0000"
    assert registration.package_id == "pkg-13-09-2026 10:15"
    [created] = fake_platform.created
    [thumbnail] = fake_platform.thumbnails          # превью ставится отдельным шагом (§7.4 п.4)
    assert thumbnail.preview == b"x"
    assert fake_platform.languages == {created.broadcast_id: "uk"}
    [form_call] = form_sender.calls
    assert (form_call.slot_id, form_call.channel_id, form_call.form_url) == (UK_SLOT, "yt_ua", FORM_SPEC["url"])
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "fake-0001-0000-0000-0000" in keys_text and "ключ передан в форму " in keys_text
    assert outcome.report is not None
    [pair_outcome] = outcome.report.outcomes
    assert (pair_outcome.kind, pair_outcome.form) == (OutcomeKind.CREATED, FormState.SENT)
    assert outcome.report_text is not None and "эфир создан, ключ получен, форма ✅" in outcome.report_text
    assert outcome.report_path is not None and outcome.report_path.exists()
    assert "pkg-13-09-2026 10:15" in Registry.load(planer_paths.registry_file).packages


def test_full_create_failure_leaves_no_registration(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_create[UK_SLOT] = PlatformError("liveStreamingNotEnabled", "на канале не включены трансляции")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert _loaded(planer_paths, UK_KEY) is None
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.ERROR
    assert "YouTube: liveStreamingNotEnabled (на канале не включены трансляции)" in (outcome.report_text or "")
    assert form_sender.calls == []


def test_failed_form_is_reported_and_never_retried(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    """Новый ключ не дошёл — код 1; следующий запуск видит тот же эфир с тем же ключом и в форму не шлёт."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    sender: FakeFormSender = FakeFormSender(confirmed=False, error="ошибка сети")
    first: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert first.exit_code == ExitCode.ERRORS
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert (registration.form_status, registration.last_error) == (FormStatus.PENDING, "ошибка сети")
    assert first.report is not None and first.report.outcomes[0].form is FormState.FAILED
    assert "не удалось передать: отправка не удалась (ошибка сети)" in planer_paths.keys_file.read_text(encoding="utf-8")
    assert "повторно планер ключ не отправит" in (first.report_text or "")
    second: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert len(sender.calls) == 1                      # повтора нет: ключ прежний
    assert second.exit_code == ExitCode.OK
    assert second.report is not None and second.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert "ключ прежний, планер его не передавал" in planer_paths.keys_file.read_text(encoding="utf-8")


def test_processed_package_stays_in_promo(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Планер пакеты не двигает и не удаляет: обработанный остаётся там же (§7.1)."""
    path: Path = make_package(
        planer_paths.promo_dir,
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
        planer_paths.promo_dir,
        generated_at="13-09-2026 10:15",
        file_name="old.bcast",
        form={**FORM_SPEC, "url": old_url},
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "uk")],
    )
    make_package(
        planer_paths.promo_dir,
        generated_at="14-09-2026 09:00",
        file_name="new.bcast",
        form={**FORM_SPEC, "url": new_url},
        slots=[make_slot("18-03-2027", "19:00", "uk")],
    )
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    urls: dict[str, str] = {call.slot_id: call.form_url for call in form_sender.calls}
    assert urls == {"17-03-2027_1900_uk": old_url, "18-03-2027_1900_uk": new_url}


def test_too_late_slot_keeps_package_in_promo(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    path: Path = make_package(
        planer_paths.promo_dir,
        slots=[make_slot("16-03-2027", "12:30", "uk"), make_slot("17-03-2027", "19:00", "uk")],
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert path.exists()
    assert outcome.report is not None
    assert "- 16-03-2027 12:30 uk — до старта меньше 60 минут" in outcome.report.skipped


def test_missing_broadcast_is_a_plain_create_whatever_the_journal_says(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Журнал помнит эфир, на площадке его нет: обычное создание, без «заново» и без истории id."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _save_registry(planer_paths, _registration())
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "recreate" not in {decision.value for decision in Decision}
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert (registration.broadcast_id, registration.stream_key) == ("fakebc00001", "fake-0001-0000-0000-0000")
    assert (registration.previous_broadcast_ids, registration.form_status) == ([], FormStatus.SENT)
    [form_call] = form_sender.calls                    # новый ключ — ровно одна отправка
    assert form_call.stream_key == "fake-0001-0000-0000-0000"
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert "создан заново" not in (outcome.report_text or "")


def test_matched_key_comes_from_the_platform_not_the_journal(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, stream_key=PLATFORM_KEY
    )
    _save_registry(planer_paths, _registration(broadcast_id=found.broadcast_id, stream_key=JOURNAL_KEY))
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert PLATFORM_KEY in keys_text and JOURNAL_KEY not in keys_text
    assert "ключ прежний, планер его не передавал" in keys_text
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert (registration.stream_key, registration.form_status) == (PLATFORM_KEY, FormStatus.NOT_SENT)
    assert form_sender.calls == [] and fake_platform.created == []
    assert outcome.report is not None
    [pair_outcome] = outcome.report.outcomes
    assert (pair_outcome.kind, pair_outcome.form) == (OutcomeKind.MATCHED, None)


def test_matched_broadcast_is_not_sent_even_if_journal_says_pending(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Прошлая отправка не подтвердилась — всё равно не шлём: повтор задвоил бы ключ у стримера."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast("yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT)
    _save_registry(
        planer_paths,
        _registration(broadcast_id=found.broadcast_id, form_status=FormStatus.PENDING, form_sent_at=None, last_error="сеть"),
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert form_sender.calls == []
    assert outcome.exit_code == ExitCode.OK            # отсутствие отправки по совпавшему эфиру — не ошибка
    assert len(_kept_key_lines(outcome)) == 1
    assert "не отправляет его в форму повторно" in (outcome.report_text or "")


def test_two_packages_with_one_slot_give_one_object_per_channel(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Слоты сливаются по slot_id ДО размножения по каналам: два пакета — один эфир."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, file_name="old.bcast", generated_at="13-09-2026 10:15", slots=[spec])
    newer: dict[str, Any] = dict(spec, title="Новое название")
    make_package(planer_paths.promo_dir, file_name="new.bcast", generated_at="14-09-2026 09:00", slots=[newer])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert len(outcome.report.outcomes) == 1
    assert len(fake_platform.created) == 1
    [created] = fake_platform.created
    assert created.marker == UK_SLOT
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None and registration.package_id == "pkg-14-09-2026 09:00"


def test_broadcast_without_stream_is_reported_and_gets_no_key(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Эфир есть, потока нет: планер привязывает поток и получает ключ (§7.3)."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=None
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.created == []                      # эфир не пересоздавался
    assert [call.broadcast_id for call in fake_platform.attached] == [found.broadcast_id]
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None and registration.stream_key
    assert len(form_sender.calls) == 1
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.STREAM_ATTACHED
    assert "поток привязан" in (outcome.report_text or "")


def test_package_fields_of_the_object_survive_the_whole_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Поля из пакета задаются в конструкторе и не переприсваиваются ни сверкой, ни действиями."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
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
    assert item.channel.id == "yt_ua"
    assert item.source_package.path.name.endswith(".bcast")
    assert item.actual is not None and item.actual.title == "Другое название"

def test_too_late_slot_reads_its_key_from_the_platform_and_writes_nothing(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Слот внутри min_lead_minutes: ключ с площадки остаётся в keys.txt, действий нет (§5.5, §7.2)."""
    make_package(planer_paths.promo_dir, slots=[make_slot("16-03-2027", "12:30", "uk")])
    soon_start: datetime = datetime.fromisoformat("2027-03-16T12:30:00+02:00")
    fake_platform.seed_broadcast(
        "yt_ua", soon_start, "Другое название", "", marker="16-03-2027_1230_uk", stream_key="soon-soon-soon-soon-soon"
    )
    _save_registry(
        planer_paths,
        _registration(slot_id="16-03-2027_1230_uk", date="16-03-2027", time="12:30", stream_key=JOURNAL_KEY),
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    keys_text: str = planer_paths.keys_file.read_text(encoding="utf-8")
    assert "soon-soon-soon-soon-soon" in keys_text and JOURNAL_KEY not in keys_text
    assert fake_platform.created == [] and fake_platform.updated == [] and fake_platform.attached == []
    assert fake_platform.settings_calls == [] and fake_platform.facts_calls == [] and fake_platform.thumbnails == []
    assert form_sender.calls == []
    assert fake_platform.stream_calls                  # площадку прочитали — только чтение
    assert outcome.report is not None and outcome.report.outcomes == []
    assert "до старта меньше 60 минут" in (outcome.report_text or "")
    assert _kept_key_lines(outcome) == []


def test_too_late_slot_without_broadcast_has_no_key_row(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("16-03-2027", "12:30", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.created == [] and form_sender.calls == []
    lines: list[str] = planer_paths.keys_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.startswith("# ") for line in lines)


def test_thumbnail_failure_is_a_warning_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk", previews=1)])
    fake_platform.fail_thumbnail["fakebc00001"] = PlatformError("forbidden", "канал не подтверждён")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None and registration.stream_key
    assert outcome.report is not None and len(outcome.report.warnings) == 1
    assert "обложка не поставлена" in (outcome.report_text or "")


def test_video_settings_failure_is_a_warning_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_settings["fakebc00001"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert _loaded(planer_paths, UK_KEY) is not None
    assert "не удалось применить настройки эфира" in (outcome.report_text or "")


def test_language_is_set_from_the_slot(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "ru")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert fake_platform.languages == {"fakebc00001": "ru"}


def test_attach_failure_stays_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=None
    )
    fake_platform.fail_attach[found.broadcast_id] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert _loaded(planer_paths, UK_KEY) is None
    assert form_sender.calls == []


def test_second_run_matches_without_writes(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Второй прогон по тому же слоту: ни одного создания и исправления."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    first_key: str | None = _loaded(planer_paths, UK_KEY).stream_key      # type: ignore[union-attr]
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert len(fake_platform.created) == 1 and fake_platform.updated == []
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.MATCHED
    assert _loaded(planer_paths, UK_KEY).stream_key == first_key         # type: ignore[union-attr]
    assert len(form_sender.calls) == 1                                   # ключ прежний — повторной отправки нет
    assert outcome.exit_code == ExitCode.OK


def test_fix_keeps_key_and_url(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, "Старое название", spec["description"], marker=UK_SLOT,
        stream_key="abcd-abcd-abcd-abcd-abcd",
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert registration.stream_key == "abcd-abcd-abcd-abcd-abcd"
    assert registration.broadcast_id == found.broadcast_id
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.FIXED
    assert form_sender.calls == []                     # исправление текстов ключ не трогает и в форму не шлёт
    assert outcome.exit_code == ExitCode.OK
    assert "обновлено. Ключ и ссылка прежние" in (outcome.report_text or "")


def test_one_failed_object_does_not_block_the_others(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(
        planer_paths.promo_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    fake_platform.fail_create[UK_SLOT] = PlatformError("liveStreamingNotEnabled", "выключены")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert _loaded(planer_paths, UK_KEY) is None
    assert _loaded(planer_paths, "18-03-2027_1900_ru|yt_ru") is not None
    assert len(fake_platform.created) == 1

def test_made_for_kids_is_fixed_and_warned(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Настройка канала может перебить флаг: планер снимает его и говорит об этом владельцу."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.made_for_kids[found.broadcast_id] = True
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert fake_platform.made_for_kids[found.broadcast_id] is False
    assert "аудитория эфира была «для детей»" in (outcome.report_text or "")


def test_audience_failure_is_a_warning_too(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_settings["fakebc00001"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert _loaded(planer_paths, UK_KEY) is not None
    assert outcome.report is not None and outcome.report.warnings


def test_age_restricted_broadcast_is_reported_but_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Возрастное ограничение через API не снимается — только сказать владельцу."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT,
    )
    fake_platform.age_restricted.add(found.broadcast_id)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "возрастное ограничение 18+" in (outcome.report_text or "")


def test_facts_are_read_once_per_object(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(
        planer_paths.promo_dir,
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
    make_package(planer_paths.promo_dir, slots=[soon, ambiguous])
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
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report.mismatches == []
    assert "Расхождения с платформой" not in (outcome.report_text or "")


def test_full_match_reports_no_mismatch_at_all(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Регрессия на живой случай 13-09-2026: время, тексты и маркер совпали — расхождений нет."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    facts = fake_platform.read_facts(make_config().channels[0], created.broadcast_id)
    assert facts.start_utc == UK_START.astimezone(timezone.utc)
    assert facts.stream_marker == UK_SLOT
    assert outcome.report is not None and outcome.report.mismatches == []
    assert "Расхождения с платформой" not in (outcome.report_text or "")


def test_description_mismatch_is_reported_shortened(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Описание в отчёт целиком не выводится: длина и начало."""
    long_text: str = "Очень длинное описание эфира. " * 40
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk", description=long_text)
    make_package(planer_paths.promo_dir, slots=[spec])
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
    text: str = outcome.report_text or ""
    assert "Расхождения с платформой" in text
    assert "описание — хотели:" in text
    assert long_text.strip() not in text          # целиком не выводится


def test_expected_and_actual_log_lines_share_keys(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Наборы ключей не должны разъезжаться: сравнивать строки иначе бессмысленно."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
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

def test_category_of_the_channel_is_used_everywhere(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Категория берётся из channels.yaml и при создании, и при настройке ресурса видео."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    assert fake_platform.categories[created.broadcast_id] == "22"


def test_video_resource_is_touched_once_per_broadcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Регрессия: раньше по каждому эфиру ресурс видео читался трижды."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    [created] = fake_platform.created
    assert fake_platform.settings_calls == [created.broadcast_id]
    assert fake_platform.settings_writes == [created.broadcast_id]


def test_second_run_writes_no_video_settings(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Совпало всё — запись не делается: чтение есть, записи нет."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
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
    make_package(planer_paths.promo_dir, slots=[spec])
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
    assert "категория — хотели: 22; на платформе: 24" in (outcome.report_text or "")


def test_live_chat_is_warned_once_per_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Чат отключается в Студии на весь канал, поэтому строка одна, а не по разу на эфир."""
    make_package(
        planer_paths.promo_dir,
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
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.report is not None
    assert [line for line in outcome.report.warnings if "живой чат" in line] == []


def test_facts_without_start_give_no_time_mismatch(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Нет времени в фактах — сравнивать не с чем; прочерк владелец читал бы как расхождение."""
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
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
        planer_paths.promo_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("18-03-2027", "19:00", "ru")],
    )
    sender: _PartialFormSender = _PartialFormSender(confirmed_slot_id=UK_SLOT)
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    confirmed: Registration | None = _loaded(planer_paths, UK_KEY)
    pending: Registration | None = _loaded(planer_paths, "18-03-2027_1900_ru|yt_ru")
    assert confirmed is not None and confirmed.form_status is FormStatus.SENT
    assert pending is not None and pending.form_status is FormStatus.PENDING
    assert pending.stream_key and pending.last_error
    text: str = outcome.report_text or ""
    assert "форма ✅" in text
    assert "форма не подтвердила запись ответа" in text
    assert "ответ формы сохранён для разбора" in text

def test_all_past_package_stays_and_is_reported(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    path: Path = make_package(planer_paths.promo_dir, slots=[make_slot("14-03-2027", "19:00", "uk")])
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
    path: Path = make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert path.exists()
    assert not planer_paths.registry_file.exists()
    assert not planer_paths.keys_file.exists()
    assert fake_platform.created == [] and form_sender.calls == []
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.CREATED
    assert "эфира нет, будет создан — не выполнено (dry-run)" in (outcome.report_text or "")


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
    _save_registry(planer_paths, _registration(form_sent_at=datetime(2027, 3, 15, 10, 0)))
    registry_before: bytes = planer_paths.registry_file.read_bytes()
    outcome: RunOutcome = _run(RunMode.STATUS, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    lines: list[str] = planer_paths.keys_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    # журнал помнит отправку 15-03-2027, но --status в него не заглядывает
    assert "ключ прежний, планер его не передавал" in lines[2] and "aaaa-aaaa-aaaa-aaaa-aaaa" in lines[2]
    assert "ключ прежний, планер его не передавал" in lines[3] and "bbbb-bbbb-bbbb-bbbb-bbbb" in lines[3]
    assert "15-03-2027 10:00" not in lines[2]
    assert outcome.report is not None
    assert [item.kind for item in outcome.report.outcomes] == [OutcomeKind.MATCHED, OutcomeKind.MATCHED]
    assert planer_paths.registry_file.read_bytes() == registry_before


@pytest.mark.parametrize("mode", [RunMode.FULL, RunMode.DRY_RUN, RunMode.STATUS])
def test_unreadable_registry_stops_the_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random, mode: RunMode,
) -> None:
    """Журнал решений не даёт, но битый файл — защита: запуск останавливается кодом 2."""
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    planer_paths.registry_file.write_text("{broken", encoding="utf-8")
    outcome: RunOutcome = _run(mode, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert (outcome.exit_code, outcome.problem) == (ExitCode.CONFIG, RunProblem.REGISTRY_UNREADABLE)
    assert outcome.report is None
    assert fake_platform.list_calls == [] and form_sender.calls == []


def test_empty_promo_exits_3(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert (outcome.exit_code, outcome.problem) == (ExitCode.PROMO_EMPTY, RunProblem.PROMO_EMPTY)
