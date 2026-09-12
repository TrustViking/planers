"""FakePlatform — площадка в памяти: используется тестами и main.py до этапа 3; после — только тестами.

Хранение по channel.id (каждый канал — отдельный YouTube-канал). Идентификаторы
детерминированные (счётчик); ключи — 5 групп по 4 символа [a-z0-9], как у YouTube (§7.4).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final

from app.config.loader import ChannelConfig
from app.package.model import Slot
from app.platforms.base import (
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    StreamInfo,
    UpcomingBroadcast,
    broadcast_url_for,
)

FAKE_STREAM_URL: Final[str] = "rtmp://a.rtmp.youtube.com/live2"
FAKE_BROADCAST_ID_TEMPLATE: Final[str] = "fakebc{number:05d}"
FAKE_STREAM_ID_TEMPLATE: Final[str] = "fakestream{number:04d}"
FAKE_STREAM_KEY_TEMPLATE: Final[str] = "fake-{number:04d}-0000-0000-0000"
FAKE_CHANNEL_ID_TEMPLATE: Final[str] = "UCfake{channel_key}"
FAKE_CHANNEL_TITLE_TEMPLATE: Final[str] = "Fake {channel_key}"
NOT_FOUND_CODE: Final[str] = "broadcastNotFound"


@dataclass(frozen=True)
class FakeCall:
    channel_id: str
    broadcast_id: str
    slot_id: str
    preview: bytes | None


class FakePlatform:
    def __init__(self) -> None:
        self._broadcasts: dict[str, dict[str, UpcomingBroadcast]] = {}
        self._streams: dict[str, dict[str, StreamInfo]] = {}
        self._counter: int = 0
        self.created: list[FakeCall] = []
        self.updated: list[FakeCall] = []
        self.list_calls: list[str] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.fail_list: dict[str, PlatformError] = {}     # channel_id → ошибка list_upcoming
        self.fail_create: dict[str, PlatformError] = {}   # slot_id → ошибка create_broadcast
        self.fail_describe: dict[str, PlatformError] = {}  # channel_id → ошибка describe_channel
        self.channel_info: dict[str, ChannelInfo] = {}     # channel_id → ответ describe_channel
        self.describe_calls: list[str] = []

    def seed_broadcast(
        self,
        channel_id: str,
        start_utc: datetime,
        title: str,
        description: str,
        marker: str | None = None,
        stream_key: str | None = None,
    ) -> UpcomingBroadcast:
        """Эфир «уже на канале». marker — название потока (поток создаётся); без marker — эфир без потока."""
        number: int = self._next_number()
        stream_id: str | None = None
        if marker is not None:
            stream_id = FAKE_STREAM_ID_TEMPLATE.format(number=number)
            self._streams.setdefault(channel_id, {})[stream_id] = StreamInfo(
                stream_id=stream_id,
                title=marker,
                ingestion_address=FAKE_STREAM_URL,
                stream_name=stream_key or FAKE_STREAM_KEY_TEMPLATE.format(number=number),
            )
        broadcast: UpcomingBroadcast = UpcomingBroadcast(
            broadcast_id=FAKE_BROADCAST_ID_TEMPLATE.format(number=number),
            start_utc=start_utc.astimezone(timezone.utc),
            title=title,
            description=description,
            stream_id=stream_id,
        )
        self._broadcasts.setdefault(channel_id, {})[broadcast.broadcast_id] = broadcast
        return broadcast

    def remove_broadcast(self, channel_id: str, broadcast_id: str) -> None:
        """Владелец удалил эфир руками."""
        self._broadcasts.get(channel_id, {}).pop(broadcast_id, None)

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """По умолчанию — детерминированный ответ по channel.id; тест может задать свой."""
        self.describe_calls.append(channel.id)
        if channel.id in self.fail_describe:
            raise self.fail_describe[channel.id]
        return self.channel_info.get(channel.id, self.default_channel_info(channel.id))

    @staticmethod
    def default_channel_info(channel_key: str) -> ChannelInfo:
        return ChannelInfo(
            youtube_channel_id=FAKE_CHANNEL_ID_TEMPLATE.format(channel_key=channel_key),
            title=FAKE_CHANNEL_TITLE_TEMPLATE.format(channel_key=channel_key),
            default_language=None,
        )

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        self.list_calls.append(channel.id)
        if channel.id in self.fail_list:
            raise self.fail_list[channel.id]
        broadcasts: list[UpcomingBroadcast] = list(self._broadcasts.get(channel.id, {}).values())
        return sorted(broadcasts, key=lambda broadcast: (broadcast.start_utc, broadcast.broadcast_id))

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        self.stream_calls.append((channel.id, stream_id))
        return self._streams.get(channel.id, {}).get(stream_id)

    def create_broadcast(self, channel: ChannelConfig, slot: Slot, preview: bytes | None) -> CreatedBroadcast:
        if slot.slot_id in self.fail_create:
            raise self.fail_create[slot.slot_id]
        number: int = self._next_number()
        stream: StreamInfo = StreamInfo(
            stream_id=FAKE_STREAM_ID_TEMPLATE.format(number=number),
            title=slot.slot_id,
            ingestion_address=FAKE_STREAM_URL,
            stream_name=FAKE_STREAM_KEY_TEMPLATE.format(number=number),
        )
        broadcast: UpcomingBroadcast = UpcomingBroadcast(
            broadcast_id=FAKE_BROADCAST_ID_TEMPLATE.format(number=number),
            start_utc=slot.start.astimezone(timezone.utc),
            title=slot.title,
            description=slot.description,
            stream_id=stream.stream_id,
        )
        self._streams.setdefault(channel.id, {})[stream.stream_id] = stream
        self._broadcasts.setdefault(channel.id, {})[broadcast.broadcast_id] = broadcast
        self.created.append(FakeCall(channel.id, broadcast.broadcast_id, slot.slot_id, preview))
        return CreatedBroadcast(
            broadcast_id=broadcast.broadcast_id,
            broadcast_url=broadcast_url_for(channel, broadcast.broadcast_id),
            stream_id=stream.stream_id,
            stream_url=stream.ingestion_address,
            stream_key=stream.stream_name,
        )

    def update_broadcast(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        slot: Slot,
        preview: bytes | None,
    ) -> None:
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.id, {}).get(broadcast_id)
        if current is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.id}")
        self._broadcasts[channel.id][broadcast_id] = replace(current, title=slot.title, description=slot.description)
        self.updated.append(FakeCall(channel.id, broadcast_id, slot.slot_id, preview))

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter
