from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config.loader import PlanerConfig
from app.package.model import Slot
from app.pipeline.plan import BroadcastSpec, ChangedField, PlannedBroadcast
from app.platforms.base import PlatformLimits, StreamInfo, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.state.registry import FormStatus, Registration
from app.tests.conftest import build_planned

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]
LIMITS: PlatformLimits = PlatformLimits(title_max_chars=20, description_max_chars=40)


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


def test_key_is_slot_and_channel(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    item: PlannedBroadcast = build_planned(slot, make_config().channels[0])
    assert item.key == f"{slot.slot_id}|yt_ua"
    assert (item.slot_id, item.language, item.date, item.time) == (slot.slot_id, "uk", slot.date, slot.time)
    assert item.account_name == "Account yt_ua"
    assert item.form is slot.form


def test_registration_round_trip(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    item: PlannedBroadcast = build_planned(slot, make_config().channels[0])
    registration: Registration = Registration(
        slot_id=slot.slot_id,
        channel_id="yt_ua",
        account_name="Account yt_ua",
        language="uk",
        date=slot.date,
        time=slot.time,
        broadcast_id="bc1",
        broadcast_url="https://www.youtube.com/watch?v=bc1",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="abcd-abcd-abcd-abcd-abcd",
        package_id=item.source_package.package_id,
        created_at=datetime(2027, 3, 1, 10, 0),
        form_status=FormStatus.SENT,
        form_sent_at=datetime(2027, 3, 1, 10, 5),
        previous_broadcast_ids=["old"],
        last_error=None,
    )
    item.apply_registration(registration)
    assert item.to_registration() == registration


def test_needs_form_follows_status_not_creation(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    assert item.needs_form is False                    # ключа нет — отправлять нечего
    item.stream_key = "abcd-abcd-abcd-abcd-abcd"
    assert item.needs_form is True                     # ключ есть, подтверждения нет
    item.form_status = FormStatus.SENT
    assert item.needs_form is False


def test_remember_broadcast_keeps_previous_id_on_recreate(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    item.broadcast_id = "oldbc"
    item.form_status = FormStatus.SENT
    item.remember_broadcast(
        broadcast_id="newbc",
        broadcast_url="https://www.youtube.com/watch?v=newbc",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="abcd-abcd-abcd-abcd-abcd",
        created_at=datetime(2027, 3, 16, 12, 0),
        is_recreate=True,
    )
    assert item.previous_broadcast_ids == ["oldbc"]
    assert item.broadcast_id == "newbc"
    assert item.form_status is FormStatus.PENDING      # новый ключ — новая отправка
    assert item.form_sent_at is None


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
