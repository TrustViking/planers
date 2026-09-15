from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from app.config.loader import PlanerConfig
from app.package.model import Slot
from app.pipeline.plan import (
    WARNING_STEP_AMBIGUOUS,
    WARNING_STEP_REPORTED_FIELD,
    ChangedField,
    Decision,
    PlannedBroadcast,
)
from app.pipeline.reconciler import MarkerParts, OrphanBroadcast, Reconciler, split_marker
from app.platforms.base import PlatformError, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import build_planned

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
    slot_ids: frozenset[str] | None = None,
) -> tuple[OrphanBroadcast, ...]:
    ids: frozenset[str] = slot_ids if slot_ids is not None else frozenset(item.slot_id for item in objects)
    return Reconciler(platform).reconcile(objects, ids, config.channels)


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
