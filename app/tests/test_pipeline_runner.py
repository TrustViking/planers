from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import FormState, OutcomeKind, PackageLineStatus
from app.paths import PlanerPaths
from app.pipeline.plan import PlannedBroadcast
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.platforms.base import PlatformError, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.state.registry import FormStatus, Registration, Registry
from app.tests.conftest import FORM_SPEC, FakeFormSender

PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]
ConfigFactory = Callable[..., PlanerConfig]
UK_SLOT: str = "17-03-2027_1900_uk"
UK_KEY: str = f"{UK_SLOT}|yt_ua"
UK_START: datetime = datetime.fromisoformat("2027-03-17T19:00:00+02:00")


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
    assert "fake-0001-0000-0000-0000" in keys_text and "форма ✅" in keys_text
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


def test_form_failure_is_remembered_for_next_run(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    sender: FakeFormSender = FakeFormSender(confirmed=False, error="ошибка сети")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, sender, now, rng)
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert (registration.form_status, registration.last_error) == (FormStatus.PENDING, "ошибка сети")
    assert outcome.report is not None and outcome.report.outcomes[0].form is FormState.FAILED
    assert "форма ❌ ошибка сети" in planer_paths.keys_file.read_text(encoding="utf-8")


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


def test_recreate_keeps_previous_broadcast_id(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    _save_registry(planer_paths, _registration())
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert registration.previous_broadcast_ids == ["oldbc"]
    assert registration.broadcast_id == "fakebc00001"
    assert registration.form_status is FormStatus.SENT
    assert outcome.report is not None and outcome.report.outcomes[0].recreated is True


def test_rebind_rewrites_registry_and_sends_form(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT, stream_key="abcd-abcd-abcd-abcd-abcd"
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert (registration.broadcast_id, registration.stream_key) == (found.broadcast_id, "abcd-abcd-abcd-abcd-abcd")
    assert registration.form_status is FormStatus.SENT
    assert fake_platform.created == []
    assert outcome.report is not None
    [pair_outcome] = outcome.report.outcomes
    assert (pair_outcome.kind, pair_outcome.rebind, pair_outcome.form) == (OutcomeKind.MATCHED, True, FormState.SENT)


def test_pending_form_is_resent_for_confirmed_broadcast(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    spec: dict[str, Any] = make_slot("17-03-2027", "19:00", "uk")
    make_package(planer_paths.promo_dir, slots=[spec])
    found: UpcomingBroadcast = fake_platform.seed_broadcast("yt_ua", UK_START, spec["title"], spec["description"], marker=UK_SLOT)
    _save_registry(planer_paths, _registration(broadcast_id=found.broadcast_id, form_status=FormStatus.PENDING, form_sent_at=None))
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert len(form_sender.calls) == 1
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None and registration.form_status is FormStatus.SENT
    assert outcome.report is not None
    assert (outcome.report.outcomes[0].kind, outcome.report.outcomes[0].form) == (OutcomeKind.MATCHED, FormState.SENT)


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
    fake_platform.seed_broadcast("yt_ua", UK_START, "Другое название", "Другое описание", marker=UK_SLOT)
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

def test_too_late_slot_keeps_its_key_and_touches_no_platform(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    """Слот внутри min_lead_minutes: ключ остаётся в keys.txt, эфир не трогаем (§5.5)."""
    make_package(planer_paths.promo_dir, slots=[make_slot("16-03-2027", "12:30", "uk")])
    _save_registry(
        planer_paths,
        _registration(
            slot_id="16-03-2027_1230_uk",
            date="16-03-2027",
            time="12:30",
            stream_key="soon-soon-soon-soon-soon",
        ),
    )
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert "soon-soon-soon-soon-soon" in planer_paths.keys_file.read_text(encoding="utf-8")
    assert fake_platform.created == [] and fake_platform.updated == []
    assert fake_platform.stream_calls == []      # по объекту площадку не спрашивали
    assert outcome.report is not None
    assert "до старта меньше 60 минут" in (outcome.report_text or "")


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


def test_language_failure_is_a_warning_not_an_error(
    planer_paths: PlanerPaths, make_package: PackageFactory, make_slot: SlotFactory, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    fake_platform.fail_language["fakebc00001"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert outcome.exit_code == ExitCode.OK
    assert _loaded(planer_paths, UK_KEY) is not None
    assert "язык эфира не записан" in (outcome.report_text or "")


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
    assert len(form_sender.calls) == 1                                   # форма уже подтверждена


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
    _save_registry(planer_paths, _registration(broadcast_id=found.broadcast_id,
                                               stream_key="abcd-abcd-abcd-abcd-abcd"))
    outcome: RunOutcome = _run(RunMode.FULL, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert [call.broadcast_id for call in fake_platform.updated] == [found.broadcast_id]
    registration: Registration | None = _loaded(planer_paths, UK_KEY)
    assert registration is not None
    assert registration.stream_key == "abcd-abcd-abcd-abcd-abcd"
    assert registration.broadcast_id == found.broadcast_id
    assert outcome.report is not None and outcome.report.outcomes[0].kind is OutcomeKind.FIXED


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
    assert "форма ✅ 15-03-2027 10:00" in lines[2] and "aaaa-aaaa-aaaa-aaaa-aaaa" in lines[2]
    assert "форма — не отправлялась" in lines[3] and "bbbb-bbbb-bbbb-bbbb-bbbb" in lines[3]
    assert outcome.report is not None
    assert [item.kind for item in outcome.report.outcomes] == [OutcomeKind.MATCHED, OutcomeKind.MATCHED]
    assert planer_paths.registry_file.read_bytes() == registry_before


def test_unreadable_registry_stops_the_run(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    planer_paths.registry_file.write_text("{broken", encoding="utf-8")
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert (outcome.exit_code, outcome.problem) == (ExitCode.CONFIG, RunProblem.REGISTRY_UNREADABLE)
    assert outcome.report is None


def test_empty_promo_exits_3(
    planer_paths: PlanerPaths, make_config: ConfigFactory,
    fake_platform: FakePlatform, form_sender: FakeFormSender, now: datetime, rng: random.Random,
) -> None:
    outcome: RunOutcome = _run(RunMode.DRY_RUN, planer_paths, make_config(), fake_platform, form_sender, now, rng)
    assert (outcome.exit_code, outcome.problem) == (ExitCode.PROMO_EMPTY, RunProblem.PROMO_EMPTY)
