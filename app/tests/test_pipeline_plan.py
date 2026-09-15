from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.loader import PlanerConfig
from app.package.model import Slot
from app.pipeline.plan import BroadcastSpec, ChangedField, Decision, OutcomeError, PlannedBroadcast
from app.platforms.base import CreatedBroadcast, PlatformLimits, StreamInfo, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import build_planned

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]
LIMITS: PlatformLimits = PlatformLimits(title_max_chars=20, description_max_chars=40)
CREATED: CreatedBroadcast = CreatedBroadcast(
    broadcast_id="newbc",
    broadcast_url="https://www.youtube.com/watch?v=newbc",
    stream_id="S9",
    stream_url="rtmp://a.rtmp.youtube.com/live2",
    stream_key="newk-newk-newk-newk-newk",
)


def _broadcast(title: str, description: str, start: datetime, stream_id: str | None = "S1") -> UpcomingBroadcast:
    return UpcomingBroadcast(
        broadcast_id="B1",
        start_utc=start,
        title=title,
        description=description,
        stream_id=stream_id,
    )


def _stream(title: str) -> StreamInfo:
    return StreamInfo(
        stream_id="S1",
        title=title,
        ingestion_address="rtmp://a.rtmp.youtube.com/live2",
        stream_name="abcd-abcd-abcd-abcd-abcd",
    )


def test_spec_from_slot_normalizes_and_trims(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(
        now + timedelta(days=1),
        "uk",
        title="  Очень длинное название эфира на канале  ",
        description="Первый абзац  \r\n\r\nВторой  ",
    )
    spec: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS)
    assert len(spec.title) <= LIMITS.title_max_chars
    assert spec.title == spec.title.strip()
    assert spec.description == "Первый абзац\n\nВторой"
    assert spec.marker == slot.slot_id
    assert spec.start_minute.tzinfo is timezone.utc
    assert (spec.start_minute.second, spec.start_minute.microsecond) == (0, 0)


def test_spec_from_platform_applies_the_same_rules(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Эфир", description="Текст")
    expected: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast("  Эфир  ", "Текст  \r\n", slot.start),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual == expected
    assert actual.diff(expected) == ()


def test_spec_from_platform_without_stream_has_empty_marker(
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    spec: BroadcastSpec = BroadcastSpec.from_platform(_broadcast("Эфир", "", slot.start, None), None, LIMITS)
    assert spec.marker == ""


def test_diff_reports_both_fields(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Новое", description="Новое описание")
    expected: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast("Старое", "Старое описание", slot.start),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual.diff(expected) == (ChangedField.TITLE, ChangedField.DESCRIPTION)


def test_diff_ignores_time_and_marker(make_slot_object: SlotFactory, now: datetime) -> None:
    """По времени и маркеру эфир опознают, а не исправляют."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    expected: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS)
    other: BroadcastSpec = BroadcastSpec(
        start_minute=expected.start_minute + timedelta(days=5),
        marker="совсем другой",
        title=expected.title,
        description=expected.description,
    )
    assert other.diff(expected) == ()


def test_long_title_is_trimmed_on_both_sides(make_slot_object: SlotFactory, now: datetime) -> None:
    """Слот длиннее лимита не должен считаться отличающимся от того, что лежит на площадке."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Очень длинное название эфира " * 5)
    expected: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast(expected.title, slot.description, slot.start),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual.diff(expected) == ()


def test_fields_come_from_slot_and_channel(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    item: PlannedBroadcast = build_planned(slot, make_config().channels[0])
    assert (item.slot_id, item.language, item.date, item.time) == (slot.slot_id, "uk", slot.date, slot.time)
    assert item.account_name == "yt_ua"
    assert item.form is slot.form


def _with_found_key(item: PlannedBroadcast) -> PlannedBroadcast:
    item.found = _broadcast(item.slot.title, item.slot.description, item.slot.start)
    item.found_stream = _stream(item.slot_id)
    item.take_found_key()
    return item


def test_object_is_born_without_key(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    """Объект знает только пакет и канал: о прошлых запусках ему нечего помнить."""
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    assert (item.broadcast_id, item.broadcast_url, item.stream_url, item.stream_key) == (None, None, None, None)
    assert (item.is_new_key, item.is_form_sent, item.is_new_key_undelivered) == (False, False, False)


def test_found_key_is_taken_from_the_platform(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = _with_found_key(
        build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    )
    assert (item.broadcast_id, item.stream_key) == ("B1", "abcd-abcd-abcd-abcd-abcd")
    assert (item.broadcast_url, item.stream_url) == ("https://www.youtube.com/watch?v=B1", "rtmp://a.rtmp.youtube.com/live2")
    assert item.is_new_key is False                    # ключ найденного эфира — не новый
    assert item.is_new_key_undelivered is False


def test_only_new_key_waits_for_the_form(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    item.take_new_key(CREATED)
    assert (item.broadcast_id, item.stream_key, item.is_new_key) == ("newbc", CREATED.stream_key, True)
    assert item.is_new_key_undelivered is True
    item.is_form_sent = True
    assert item.is_new_key_undelivered is False


def test_kept_key_is_match_or_update_without_new_key(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = _with_found_key(
        build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    )
    for decision, expected in ((Decision.MATCH, True), (Decision.UPDATE, True), (Decision.TOO_LATE, False)):
        item.decision = decision
        assert item.has_kept_key is expected
    item.decision = Decision.UPDATE
    item.error = OutcomeError(origin="youtube", code="forbidden")
    assert item.has_kept_key is False                  # исправление не удалось — это ошибка, не прежний ключ
    item.error = None
    item.take_new_key(CREATED)                         # привязка потока: ключ новый
    assert item.has_kept_key is False


def test_package_fields_survive_platform_data(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    channel: Any = make_config().channels[0]
    item: PlannedBroadcast = build_planned(slot, channel)
    expected: BroadcastSpec = item.expected
    item.found = _broadcast("Чужое", "Чужое", slot.start)
    item.actual = BroadcastSpec.from_platform(item.found, _stream("маркер"), FakePlatform().limits)
    assert item.slot is slot
    assert item.channel is channel
    assert item.expected is expected
    assert item.source_package.package_id == "pkg-test"
