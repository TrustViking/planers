from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from app.config.loader import ChannelConfig, PlanerConfig
from app.package.model import Slot
from app.platforms.base import (
    PlatformError,
    StreamInfo,
    UpcomingBroadcast,
    picture_sha,
    placeholder_sha_from_description,
)
from app.pipeline.plan import BroadcastSpec
from app.platforms.fake import FakeCall, FakePlatform
from app.tests.conftest import build_config

# Регулярка ключа YouTube (ТЗ §7.4): фейковые ключи должны её проходить.
YOUTUBE_KEY_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9]{4}(-[a-z0-9]{4}){3,4}$")


def _channels(make_config: Callable[..., PlanerConfig]) -> tuple[ChannelConfig, ChannelConfig]:
    config: PlanerConfig = make_config()
    return config.channels[0], config.channels[1]


def test_seed_with_marker_creates_bound_stream(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    channel, _ = _channels(make_config)
    seeded: UpcomingBroadcast = fake_platform.seed_broadcast(
        channel.account_name, now + timedelta(days=1), "Название", "Описание", marker="17-03-2027_1200_uk"
    )
    [listed] = fake_platform.list_upcoming(channel)
    assert listed == seeded
    assert listed.start_utc.utcoffset() == timedelta(0)
    assert listed.stream_id is not None
    stream: StreamInfo | None = fake_platform.get_stream(channel, listed.stream_id)
    assert stream is not None
    assert stream.title == "17-03-2027_1200_uk"
    assert YOUTUBE_KEY_PATTERN.fullmatch(stream.stream_name)


def test_seed_without_marker_has_no_stream(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    channel, _ = _channels(make_config)
    fake_platform.seed_broadcast(channel.account_name, now, "Ручной", "")
    [listed] = fake_platform.list_upcoming(channel)
    assert listed.stream_id is None


def _spec(platform: FakePlatform, slot: Slot) -> BroadcastSpec:
    """Площадка получает спеку, а не слот (ТЗ §7.4)."""
    config: PlanerConfig = build_config()
    return BroadcastSpec.from_slot(slot, platform.limits, config.channels[0], config.settings)


def test_create_broadcast_is_deterministic_and_marked(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    make_slot_object: Callable[..., Slot],
    now: datetime,
) -> None:
    channel, _ = _channels(make_config)
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    created = fake_platform.create_broadcast(channel, _spec(fake_platform, slot))
    assert created.broadcast_id == "fakebc00001"
    assert created.broadcast_url == "https://www.youtube.com/watch?v=fakebc00001"
    assert created.stream_url == "rtmp://a.rtmp.youtube.com/live2"
    assert created.stream_key == "fake-0001-0000-0000-0000"
    assert YOUTUBE_KEY_PATTERN.fullmatch(created.stream_key)
    [listed] = fake_platform.list_upcoming(channel)
    assert (listed.title, listed.description, listed.start_utc) == (slot.title, slot.description, slot.start)
    stream: StreamInfo | None = fake_platform.get_stream(channel, created.stream_id)
    assert stream is not None and stream.title == slot.slot_id
    assert fake_platform.created == [FakeCall(channel.account_name, created.broadcast_id, slot.slot_id, None)]


def test_update_changes_texts_and_unknown_broadcast_fails(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    make_slot_object: Callable[..., Slot],
    now: datetime,
) -> None:
    channel, _ = _channels(make_config)
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Новое", description="Новое описание")
    seeded: UpcomingBroadcast = fake_platform.seed_broadcast(channel.account_name, slot.start, "Старое", "Старое описание")
    fake_platform.update_broadcast(channel, seeded.broadcast_id, _spec(fake_platform, slot))
    [listed] = fake_platform.list_upcoming(channel)
    assert (listed.title, listed.description) == ("Новое", "Новое описание")
    assert fake_platform.updated == [FakeCall(channel.account_name, seeded.broadcast_id, slot.slot_id, None)]
    with pytest.raises(PlatformError):
        fake_platform.update_broadcast(channel, "nope", _spec(fake_platform, slot))


def test_failures_are_configurable(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    make_slot_object: Callable[..., Slot],
    now: datetime,
) -> None:
    channel, _ = _channels(make_config)
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    fake_platform.fail_list[channel.account_name] = PlatformError("quotaExceeded", "квота")
    fake_platform.fail_create[slot.slot_id] = PlatformError("liveStreamingNotEnabled", "трансляции выключены")
    with pytest.raises(PlatformError, match="quotaExceeded"):
        fake_platform.list_upcoming(channel)
    with pytest.raises(PlatformError, match="liveStreamingNotEnabled"):
        fake_platform.create_broadcast(channel, _spec(fake_platform, slot))
    assert fake_platform.created == []


def test_channels_are_isolated(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    first, second = _channels(make_config)
    fake_platform.seed_broadcast(first.account_name, now, "Первый", "")
    assert fake_platform.list_upcoming(second) == []
    assert fake_platform.list_calls == [second.account_name]


def test_new_broadcast_shows_channel_placeholder_until_thumbnail_is_set(
    fake_platform: FakePlatform,
    make_config: Callable[..., PlanerConfig],
    make_slot_object: Callable[..., Slot],
    now: datetime,
) -> None:
    """Как у площадки: картинка нового эфира — заглушка канала, её отпечаток — в описании потока."""
    channel, _ = _channels(make_config)
    created = fake_platform.create_broadcast(channel, _spec(fake_platform, make_slot_object(now + timedelta(days=1), "uk")))
    [listed] = fake_platform.list_upcoming(channel)
    placeholder: str = FakePlatform.placeholder_of(channel.account_name)
    assert listed.thumbnail_sha == placeholder
    stream: StreamInfo | None = fake_platform.get_stream(channel, created.stream_id)
    assert stream is not None and placeholder_sha_from_description(stream.description) == placeholder
    fake_platform.set_thumbnail(channel, created.broadcast_id, b"preview")
    [listed] = fake_platform.list_upcoming(channel)
    assert listed.thumbnail_sha == picture_sha(b"preview")
