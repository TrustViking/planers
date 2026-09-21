from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config.loader import PlanerConfig
from app.package.model import Slot
from app.pipeline.plan import (
    WARNING_STEP_AMBIGUOUS,
    WARNING_STEP_REPORTED_FIELD,
    ChangedField,
    Decision,
    PlannedBroadcast,
)
from app.pipeline.reconciler import MarkerParts, OrphanBroadcast, OrphanKind, Reconciler, split_marker
from app.platforms.base import PLACEHOLDER_TOKEN, PlatformError, UpcomingBroadcast
from app.form.base import FormError
from app.platforms.channel import Channel, ChannelStatus
from app.platforms.fake import FakePlatform
from app.tests.conftest import FIXED_NOW, RecordingProgress, build_config, build_planned

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]


def _objects(config: PlanerConfig, *slots: Slot) -> list[PlannedBroadcast]:
    return [
        build_planned(slot, channel)
        for slot in slots
        for channel in config.channels
        if slot.language in channel.languages
    ]


def _too_late(item: PlannedBroadcast) -> PlannedBroadcast:
    """Как его строит отбор (app/pipeline/selection.py): флаг и решение TOO_LATE."""
    item.is_too_late = True
    item.decision = Decision.TOO_LATE
    return item


def _reconcile(
    platform: FakePlatform,
    config: PlanerConfig,
    *objects: PlannedBroadcast,
    known_slots: dict[str, datetime] | None = None,
) -> tuple[OrphanBroadcast, ...]:
    known: dict[str, datetime] = (
        known_slots if known_slots is not None else {item.slot_id: item.slot.start for item in objects}
    )
    return Reconciler(platform).reconcile(objects, known, config.channels)


def _seed_like(platform: FakePlatform, channel_id: str, slot: Slot, **overrides: object) -> UpcomingBroadcast:
    values: dict[str, object] = dict(
        start_utc=slot.start,
        title=slot.title,
        description=slot.description,
        marker=slot.slot_id,
    )
    values.update(overrides)
    return platform.seed_broadcast(channel_id, **values)  # type: ignore[arg-type]


def test_no_broadcast_means_create(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.CREATE


def test_fields_the_platform_did_not_return_are_logged_once_per_broadcast(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Список эфиров YouTube категорию не отдаёт: сверки по ней нет — это видно в логе, а не пропадает молча."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot)
    [item] = _objects(make_config(), slot)
    with caplog.at_level("INFO", logger="planer.reconciler"):
        _reconcile(fake_platform, make_config(), item)
    lines: list[str] = [message for message in caplog.messages if message.startswith("spec_fields_not_compared")]
    # картинку эфира площадка не отдала (посеянный эфир без картинки) — обложка тоже не сверялась
    assert lines == [f'spec_fields_not_compared slot_id={slot.slot_id} channel="yt_ua" handle=@yt_ua fields=category,thumbnail']
    assert [record.levelname for record in caplog.records if "spec_fields_not_compared" in record.getMessage()] == ["INFO"]


def test_two_planer_broadcasts_of_one_minute_on_one_channel_are_told_apart_by_marker(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Многоязычный канал: uk и ru в одну минуту — каждый опознан своим маркером, оба MATCH, не AMBIGUOUS."""
    start: datetime = now + timedelta(days=1)
    uk: Slot = make_slot_object(start, "uk")
    ru: Slot = make_slot_object(start, "ru")
    config: PlanerConfig = make_config([("multi", ["uk", "ru"])])
    uk_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "multi", uk)
    ru_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "multi", ru)
    objects: list[PlannedBroadcast] = _objects(config, uk, ru)
    _reconcile(fake_platform, config, *objects)
    by_slot: dict[str, PlannedBroadcast] = {item.slot_id: item for item in objects}
    assert by_slot[uk.slot_id].found == uk_broadcast and by_slot[ru.slot_id].found == ru_broadcast
    assert {item.decision for item in objects} == {Decision.MATCH}
    assert all(not item.ambiguous_urls for item in objects)


def test_marked_broadcast_with_same_texts_matches(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    seeded: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, stream_key="abcd-abcd-abcd-abcd-abcd")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.MATCH
    assert item.found == seeded
    assert item.found_stream is not None and item.found_stream.stream_name == "abcd-abcd-abcd-abcd-abcd"
    # ключ и ссылка — те, что сейчас на площадке; эфир с нашей меткой совпал — в форму ключ не идёт
    assert (item.broadcast_id, item.stream_key, item.should_send_key) == (
        seeded.broadcast_id,
        "abcd-abcd-abcd-abcd-abcd",
        False,
    )


def test_changed_description_means_update(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot, description="Старое описание")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.UPDATE
    assert item.changed_fields == (ChangedField.DESCRIPTION,)


def test_outer_spaces_and_line_endings_do_not_count(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(
        now + timedelta(days=1),
        "uk",
        title="Эфир",
        description="Первый абзац\n\nВторой",
    )
    _seed_like(fake_platform, "yt_ua", slot, title="  Эфир  ", description="Первый абзац  \r\n\r\nВторой\r\n")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.MATCH
    assert item.changed_fields == ()


def test_title_longer_than_limit_does_not_loop_forever(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Обрезка применяется к обеим сторонам: второй прогон обязан дать MATCH."""
    long_title: str = "Очень длинное название эфира " * 10
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title=long_title)
    [first] = _objects(make_config(), slot)
    _seed_like(fake_platform, "yt_ua", slot, title=first.expected.title)
    _reconcile(fake_platform, make_config(), first)
    assert len(first.expected.title) <= fake_platform.limits.title_max_chars
    assert first.decision is Decision.MATCH
    [second] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), second)
    assert second.decision is Decision.MATCH


def test_decisions_have_no_recreate_branch() -> None:
    """Эфира нет — всегда CREATE: различать «впервые» и «заново» планеру нечем и незачем."""
    assert {decision.value for decision in Decision} == {
        "create",
        "match",
        "update",
        "no_stream",
        "too_late",
        "ambiguous",
        "not_admitted",
        "error",
    }


def test_two_languages_on_one_minute_each_find_their_marker(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    start: datetime = now + timedelta(days=1)
    ru: Slot = make_slot_object(start, "ru")
    en: Slot = make_slot_object(start, "en")
    en_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ru", en)
    ru_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ru", ru)
    objects: list[PlannedBroadcast] = _objects(make_config(), ru, en)
    _reconcile(fake_platform, make_config(), *objects)
    found: dict[str, str | None] = {
        item.slot_id: item.found.broadcast_id if item.found else None for item in objects
    }
    assert found == {ru.slot_id: ru_broadcast.broadcast_id, en.slot_id: en_broadcast.broadcast_id}
    assert {item.decision for item in objects} == {Decision.MATCH}
    assert len(fake_platform.stream_calls) == len(set(fake_platform.stream_calls))


def test_single_manual_broadcast_is_adopted_with_its_platform_key(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Ручной эфир без метки опознан: метка планера — исправимое поле, значит UPDATE."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    manual: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, marker="Мой поток")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.UPDATE
    assert item.changed_fields == (ChangedField.MARKER,)
    assert item.found == manual
    assert item.found_stream is not None
    assert item.stream_key == item.found_stream.stream_name


def test_broadcast_without_bound_stream_gives_no_stream(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Эфир есть, потока нет: ключ взять неоткуда — ни MATCH, ни UPDATE."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    orphaned: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, marker=None)
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.NO_STREAM
    assert item.found == orphaned
    assert item.found_stream is None


def test_two_manual_broadcasts_are_ambiguous(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    first: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, marker=None)
    second: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, marker="Мой поток")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.AMBIGUOUS
    # планер не выбирает, но называет все эфиры-кандидаты: предупреждение объекта со ссылками
    assert item.ambiguous_urls == (
        f"https://www.youtube.com/watch?v={first.broadcast_id}",
        f"https://www.youtube.com/watch?v={second.broadcast_id}",
    )
    assert [warning.step for warning in item.warnings] == [WARNING_STEP_AMBIGUOUS]


def test_privacy_only_difference_means_update(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Живой прогон 15-09: владелец поставил Private, тексты совпадали — раньше это было decision=match."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot, privacy="private")
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.UPDATE
    assert (item.changed_fields, item.reported_fields) == ((ChangedField.PRIVACY,), ())


def test_auto_start_difference_is_reported_but_keeps_the_decision(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Автостарт через API не исправить (monitorStream): решение прежнее, но владелец узнаёт."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot, auto_start=False)
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.MATCH
    assert (item.changed_fields, item.reported_fields) == ((), (ChangedField.AUTO_START,))
    assert [(warning.step, warning.code) for warning in item.warnings] == [(WARNING_STEP_REPORTED_FIELD, "auto_start")]


def test_list_failure_is_isolated_to_its_channel(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    uk: Slot = make_slot_object(now + timedelta(days=1), "uk")
    ru: Slot = make_slot_object(now + timedelta(days=1), "ru")
    fake_platform.fail_list["yt_ua"] = PlatformError("quotaExceeded", "квота исчерпана")
    objects: list[PlannedBroadcast] = _objects(make_config(), uk, ru)
    _reconcile(fake_platform, make_config(), *objects)
    by_slot: dict[str, PlannedBroadcast] = {item.slot_id: item for item in objects}
    assert by_slot[uk.slot_id].decision is Decision.ERROR
    assert by_slot[uk.slot_id].error is not None and by_slot[uk.slot_id].error.code == "quotaExceeded"
    assert by_slot[ru.slot_id].decision is Decision.CREATE


def test_list_upcoming_is_called_once_per_channel(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    start: datetime = now + timedelta(days=1)
    slots: list[Slot] = [make_slot_object(start, "uk"), make_slot_object(start, "ru"), make_slot_object(start, "en")]
    _reconcile(fake_platform, make_config(), *_objects(make_config(), *slots))
    assert fake_platform.list_calls == ["yt_ua", "yt_ru"]


def test_too_late_object_only_reads_its_key(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """too_late: опознание и ключ с площадки; решение TOO_LATE, тексты не сравниваются."""
    soon: Slot = make_slot_object(now + timedelta(minutes=30), "uk")
    later: Slot = make_slot_object(now + timedelta(days=1), "uk")
    seeded: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", soon, title="Другое", stream_key="soon-soon-soon-soon-soon")
    _seed_like(fake_platform, "yt_ua", later)
    [soon_item] = _objects(make_config(), soon)
    [later_item] = _objects(make_config(), later)
    _reconcile(fake_platform, make_config(), _too_late(soon_item), later_item)
    assert soon_item.decision is Decision.TOO_LATE
    assert (soon_item.found, soon_item.stream_key, soon_item.should_send_key) == (seeded, "soon-soon-soon-soon-soon", False)
    assert (soon_item.actual, soon_item.changed_fields) == (None, ())
    assert later_item.decision is Decision.MATCH
    assert fake_platform.list_calls == ["yt_ua", "yt_ru"]                        # по-прежнему раз на канал
    assert len(fake_platform.stream_calls) == len(set(fake_platform.stream_calls))  # тот же кеш потоков


def test_too_late_without_broadcast_has_no_key_and_no_create(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    soon: Slot = make_slot_object(now + timedelta(minutes=30), "uk")
    [item] = _objects(make_config(), soon)
    _reconcile(fake_platform, make_config(), _too_late(item))
    assert item.decision is Decision.TOO_LATE
    assert (item.found, item.stream_key) == (None, None)


def test_too_late_is_not_an_error_when_the_channel_fails(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    soon: Slot = make_slot_object(now + timedelta(minutes=30), "uk")
    fake_platform.fail_list["yt_ua"] = PlatformError("quotaExceeded", "квота исчерпана")
    [item] = _objects(make_config(), soon)
    _reconcile(fake_platform, make_config(), _too_late(item))
    assert (item.decision, item.error, item.stream_key) == (Decision.TOO_LATE, None, None)


def test_marker_of_slot_outside_the_map_is_an_orphan(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    moved: Slot = make_slot_object(now + timedelta(days=3), "uk")
    moved_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", moved)
    [item] = _objects(make_config(), slot)
    [orphan] = _reconcile(fake_platform, make_config(), item)
    assert (orphan.channel.account_name, orphan.broadcast, orphan.marker) == ("yt_ua", moved_broadcast, moved.slot_id)
    assert split_marker(orphan.marker) == MarkerParts(date="19-03-2027", time="12:00", language="uk")
    assert item.decision is Decision.CREATE


def test_manual_broadcast_at_another_time_is_ignored(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    fake_platform.seed_broadcast("yt_ua", now + timedelta(days=2), "Ручной", "", marker="Мой поток")
    [item] = _objects(make_config(), slot)
    assert _reconcile(fake_platform, make_config(), item) == ()
    assert item.decision is Decision.CREATE


def test_split_marker_rejects_non_markers() -> None:
    assert split_marker("Мой поток") is None
    assert split_marker("99-99-2027_1900_uk") is None
    assert split_marker("17-03-2027_1900_uk") == MarkerParts(date="17-03-2027", time="19:00", language="uk")


def test_progress_brackets_each_channel_read(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """«Запрашиваю» — до list_upcoming (вход в канал — внутри него), «эфиров N» — после ответа."""
    uk: Slot = make_slot_object(now + timedelta(days=1), "uk")
    ru: Slot = make_slot_object(now + timedelta(days=1), "ru")
    _seed_like(fake_platform, "yt_ru", ru)
    config: PlanerConfig = make_config()
    progress: RecordingProgress = RecordingProgress(fake_platform)
    objects: list[PlannedBroadcast] = _objects(config, uk, ru)
    Reconciler(fake_platform, progress=progress).reconcile(
        objects, {uk.slot_id: uk.start, ru.slot_id: ru.start}, config.channels
    )
    assert progress.calls == [
        ("channel_read_started", "yt_ua", 0),
        ("channel_read_done", "yt_ua", 0, 1),
        ("channel_read_started", "yt_ru", 1),
        ("channel_read_done", "yt_ru", 1, 2),
    ]


def test_failed_channel_read_has_start_but_no_done(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
) -> None:
    """Сбой канала владелец увидит во «Внимание»; строки «эфиров N» по нему нет."""
    config: PlanerConfig = make_config()
    fake_platform.fail_list["yt_ua"] = PlatformError("quotaExceeded", "квота исчерпана")
    progress: RecordingProgress = RecordingProgress()
    Reconciler(fake_platform, progress=progress).marked_broadcasts(config.channels)
    assert progress.calls == [
        ("channel_read_started", "yt_ua"),
        ("channel_read_started", "yt_ru"),
        ("channel_read_done", "yt_ru", 0),
    ]


# --- задача 5k: обложка — сверяемое поле; «обложки нет» = картинка эфира совпадает с заглушкой канала

PLACEHOLDER: str = "044eb0835668"
OWN_PICTURE: str = "aaaaaaaaaaaa"


def _with_preview(slot: Slot) -> Slot:
    return replace(slot, previews=(f"previews/{slot.slot_id}_1.jpg",))


def _slots_with_previews(make_slot_object: SlotFactory, now: datetime, count: int) -> list[Slot]:
    return [_with_preview(make_slot_object(now + timedelta(days=1, hours=hour), "uk")) for hour in range(count)]


def test_picture_equal_to_stream_token_is_a_missing_thumbnail(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    [slot] = _slots_with_previews(make_slot_object, now, 1)
    _seed_like(
        fake_platform, "yt_ua", slot,
        picture=PLACEHOLDER, stream_description="Ключ планера; " + PLACEHOLDER_TOKEN.format(sha=PLACEHOLDER),
    )
    [item] = _objects(make_config(), slot)
    _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.UPDATE
    assert item.changed_fields == (ChangedField.THUMBNAIL,)


def test_same_picture_on_two_broadcasts_of_a_channel_is_a_placeholder(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Эфиры Maria Kamenskay, созданные до 5k: токена в описании потока нет, но заглушка у всех одна."""
    slots: list[Slot] = _slots_with_previews(make_slot_object, now, 2)
    for slot in slots:
        _seed_like(fake_platform, "yt_ua", slot, picture=PLACEHOLDER)
    items: list[PlannedBroadcast] = _objects(make_config(), *slots)
    _reconcile(fake_platform, make_config(), *items)
    assert [(item.decision, item.changed_fields) for item in items] == [
        (Decision.UPDATE, (ChangedField.THUMBNAIL,)),
        (Decision.UPDATE, (ChangedField.THUMBNAIL,)),
    ]


def test_unique_picture_without_tokens_is_an_own_thumbnail(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slots: list[Slot] = _slots_with_previews(make_slot_object, now, 2)
    _seed_like(fake_platform, "yt_ua", slots[0], picture=OWN_PICTURE)
    _seed_like(fake_platform, "yt_ua", slots[1], picture="bbbbbbbbbbbb")
    items: list[PlannedBroadcast] = _objects(make_config(), *slots)
    _reconcile(fake_platform, make_config(), *items)
    assert [item.decision for item in items] == [Decision.MATCH, Decision.MATCH]


def test_picture_that_did_not_download_is_not_compared(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    [slot] = _slots_with_previews(make_slot_object, now, 1)
    _seed_like(fake_platform, "yt_ua", slot, picture=None)
    [item] = _objects(make_config(), slot)
    with caplog.at_level("INFO", logger="planer.reconciler"):
        _reconcile(fake_platform, make_config(), item)
    assert item.decision is Decision.MATCH
    [line] = [message for message in caplog.messages if message.startswith("spec_fields_not_compared")]
    assert line.endswith("thumbnail")


@pytest.mark.parametrize("case", ["set_thumbnail_off", "slot_without_previews"])
def test_thumbnail_is_not_compared_when_the_planer_does_not_set_it(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
    case: str,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    if case == "set_thumbnail_off":
        slot = _with_preview(slot)
    _seed_like(
        fake_platform, "yt_ua", slot,
        picture=PLACEHOLDER, stream_description=PLACEHOLDER_TOKEN.format(sha=PLACEHOLDER),
    )
    config: PlanerConfig = make_config(set_thumbnail=case != "set_thumbnail_off")
    channel = config.channels[0]
    item: PlannedBroadcast = build_planned(slot, channel, settings=config.settings)
    assert item.expected.has_own_thumbnail is None
    _reconcile(fake_platform, config, item)
    assert item.decision is Decision.MATCH


def test_same_picture_on_different_channels_is_not_a_placeholder(
    fake_platform: FakePlatform,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    config: PlanerConfig = build_config(channels=(("yt_ua", ["uk"]), ("yt_ua2", ["uk"])))
    [slot] = _slots_with_previews(make_slot_object, now, 1)
    _seed_like(fake_platform, "yt_ua", slot, picture=OWN_PICTURE)
    _seed_like(fake_platform, "yt_ua2", slot, picture=OWN_PICTURE)
    items: list[PlannedBroadcast] = _objects(config, slot)
    _reconcile(fake_platform, config, *items)
    assert [(item.account_name, item.decision) for item in items] == [
        ("yt_ua", Decision.MATCH),
        ("yt_ua2", Decision.MATCH),
    ]


# --- не допущенные объекты

START: datetime = FIXED_NOW + timedelta(days=1)


def _not_admitted_by_channel(item: PlannedBroadcast, status: ChannelStatus) -> PlannedBroadcast:
    item.admit(Channel(config=item.channel, token_file=Path("t.json"), status=status), None, None)
    return item


def test_channel_not_ready_is_never_asked(
    make_config: ConfigFactory, make_slot_object: SlotFactory, fake_platform: FakePlatform
) -> None:
    """Канал не READY: ни list_upcoming, ни get_stream; его объекты — NOT_ADMITTED, другой канал сверен."""
    config: PlanerConfig = make_config()
    ua_slot: Slot = make_slot_object(START, "uk")
    ru_slot: Slot = make_slot_object(START, "ru")
    fake_platform.seed_broadcast("yt_ua", START, ua_slot.title, ua_slot.description, marker=ua_slot.slot_id)
    ua, ru = _objects(config, ua_slot, ru_slot)
    _not_admitted_by_channel(ua, ChannelStatus.REFUSED)
    _not_admitted_by_channel(ru, ChannelStatus.READY)
    _reconcile(fake_platform, config, ua, ru)
    assert fake_platform.list_calls == ["yt_ru"]
    assert all(call[0] != "yt_ua" for call in fake_platform.stream_calls)
    assert (ua.decision, ua.error, ua.stream_key) == (Decision.NOT_ADMITTED, None, None)
    assert ru.decision is Decision.CREATE


def test_object_not_admitted_by_form_only_reads_its_key(
    make_config: ConfigFactory, make_slot_object: SlotFactory, fake_platform: FakePlatform
) -> None:
    """Канал READY, форма объект не принимает: эфир опознан, ключ и ссылка взяты, решение NOT_ADMITTED."""
    config: PlanerConfig = make_config()
    slot: Slot = make_slot_object(START, "uk")
    fake_platform.seed_broadcast("yt_ua", START, "Старое название", slot.description, marker=slot.slot_id)
    [item] = _objects(config, slot)
    item.admit(
        Channel(config=item.channel, token_file=Path("t.json"), status=ChannelStatus.READY),
        None,
        FormError("structureUnreadable", "нет скрипта"),
    )
    _reconcile(fake_platform, config, item)
    assert item.decision is Decision.NOT_ADMITTED
    assert item.stream_key is not None and item.broadcast_url is not None
    assert item.changed_fields == () and item.warnings == []
    assert fake_platform.created == [] and fake_platform.updated == []


def test_not_admitted_object_does_not_become_ambiguous(
    make_config: ConfigFactory, make_slot_object: SlotFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    slot: Slot = make_slot_object(START, "uk")
    for title in ("Ручной 1", "Ручной 2"):
        fake_platform.seed_broadcast("yt_ua", START, title, "", marker="ручной ключ")
    [item] = _objects(config, slot)
    item.admit(None, None, FormError("structureUnreadable", "нет скрипта"))
    _reconcile(fake_platform, config, item)
    assert item.decision is Decision.NOT_ADMITTED and item.ambiguous_urls == () and item.stream_key is None


def test_marker_of_known_slot_on_another_minute_is_moved(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """5o-A: владелец перенёс эфир планера в Студии — слот известен, минута другая: «перенесён», не трогаем."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    moved: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", slot.start + timedelta(days=2), slot.title, slot.description, marker=slot.slot_id
    )
    [item] = _objects(make_config(), slot)
    [orphan] = _reconcile(fake_platform, make_config(), item)
    assert (orphan.kind, orphan.broadcast, orphan.marker) == (OrphanKind.MOVED, moved, slot.slot_id)
    assert item.decision is Decision.CREATE
    assert fake_platform.updated == []


def test_past_slot_on_its_minute_is_not_an_orphan_but_moved_one_is(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Правило 5m-E цело: эфир прошедшего слота на своей минуте — не сирота; на чужой — «перенесён»."""
    future: Slot = make_slot_object(now + timedelta(days=1), "uk")
    past: Slot = make_slot_object(now - timedelta(hours=1), "uk")
    moved_past: Slot = make_slot_object(now - timedelta(hours=2), "uk")
    _seed_like(fake_platform, "yt_ua", past)
    moved: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", now + timedelta(days=5), moved_past.title, moved_past.description, marker=moved_past.slot_id
    )
    [item] = _objects(make_config(), future)
    known: dict[str, datetime] = {future.slot_id: future.start, past.slot_id: past.start, moved_past.slot_id: moved_past.start}
    orphans: tuple[OrphanBroadcast, ...] = _reconcile(fake_platform, make_config(), item, known_slots=known)
    assert [(orphan.kind, orphan.broadcast) for orphan in orphans] == [(OrphanKind.MOVED, moved)]


def test_found_broadcast_is_not_repeated_as_moved(
    fake_platform: FakePlatform,
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    """Эфир, опознанный объектом, в «Перенесён или отменён?» не повторяется; дубль метки на другой минуте — да."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    found: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot)
    duplicate: UpcomingBroadcast = fake_platform.seed_broadcast(
        "yt_ua", slot.start + timedelta(hours=3), slot.title, slot.description, marker=slot.slot_id
    )
    [item] = _objects(make_config(), slot)
    orphans: tuple[OrphanBroadcast, ...] = _reconcile(fake_platform, make_config(), item)
    assert item.found == found
    assert [orphan.broadcast for orphan in orphans] == [duplicate]
