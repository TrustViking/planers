"""FakePlatform — площадка в памяти: используется тестами и main.py до этапа 3; после — только тестами.

Хранение по channel.id (каждый канал — отдельный YouTube-канал). Идентификаторы
детерминированные (счётчик); ключи — 5 групп по 4 символа [a-z0-9], как у YouTube (§7.4).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final

from app.config.loader import ChannelConfig
from app.pipeline.plan import BroadcastSpec
from app.platforms.base import (
    BroadcastFacts,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
    broadcast_url_for,
)

FAKE_STREAM_URL: Final[str] = "rtmp://a.rtmp.youtube.com/live2"
FAKE_BROADCAST_ID_TEMPLATE: Final[str] = "fakebc{number:05d}"
FAKE_STREAM_ID_TEMPLATE: Final[str] = "fakestream{number:04d}"
FAKE_STREAM_KEY_TEMPLATE: Final[str] = "fake-{number:04d}-0000-0000-0000"
FAKE_CHANNEL_ID_TEMPLATE: Final[str] = "UCfake{channel_key}"
FAKE_CHANNEL_TITLE_TEMPLATE: Final[str] = "Fake {channel_key}"
NOT_FOUND_CODE: Final[str] = "broadcastNotFound"
# Те же лимиты, что у YouTube: тесты должны ловить реальное поведение обрезки.
FAKE_TITLE_MAX_CHARS: Final[int] = 100
FAKE_DESCRIPTION_MAX_CHARS: Final[int] = 5000


@dataclass(frozen=True)
class FakeCall:
    channel_id: str
    broadcast_id: str
    marker: str
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
        self.fail_update: dict[str, PlatformError] = {}    # broadcast_id → ошибка update_broadcast
        self.fail_attach: dict[str, PlatformError] = {}    # broadcast_id → ошибка attach_stream
        self.fail_settings: dict[str, PlatformError] = {}  # broadcast_id → ошибка apply_video_settings
        self.fail_thumbnail: dict[str, PlatformError] = {}  # broadcast_id → ошибка set_thumbnail
        self.fail_facts: dict[str, PlatformError] = {}      # broadcast_id → ошибка read_facts
        self.made_for_kids: dict[str, bool] = {}           # broadcast_id → как стоит на площадке
        self.age_restricted: set[str] = set()              # broadcast_id с ytAgeRestricted
        self.settings_calls: list[str] = []      # обращения к ресурсу видео на чтение
        self.settings_writes: list[str] = []     # и на запись
        self.categories: dict[str, str] = {}     # broadcast_id → категория на площадке
        self.live_chat_ids: dict[str, str] = {}  # broadcast_id → id чата, если он заведён
        self.facts_calls: list[str] = []
        self.facts_override: dict[str, BroadcastFacts] = {}  # broadcast_id → готовый ответ read_facts
        self.thumbnails: list[FakeCall] = []
        self.attached: list[FakeCall] = []
        self.languages: dict[str, str] = {}                # broadcast_id → записанный язык
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

    @property
    def limits(self) -> PlatformLimits:
        return PlatformLimits(
            title_max_chars=FAKE_TITLE_MAX_CHARS,
            description_max_chars=FAKE_DESCRIPTION_MAX_CHARS,
        )

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

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        if spec.marker in self.fail_create:
            raise self.fail_create[spec.marker]
        number: int = self._next_number()
        stream: StreamInfo = StreamInfo(
            stream_id=FAKE_STREAM_ID_TEMPLATE.format(number=number),
            title=spec.marker,
            ingestion_address=FAKE_STREAM_URL,
            stream_name=FAKE_STREAM_KEY_TEMPLATE.format(number=number),
        )
        broadcast: UpcomingBroadcast = UpcomingBroadcast(
            broadcast_id=FAKE_BROADCAST_ID_TEMPLATE.format(number=number),
            start_utc=spec.start_minute,
            title=spec.title,
            description=spec.description,
            stream_id=stream.stream_id,
        )
        self._streams.setdefault(channel.id, {})[stream.stream_id] = stream
        self._broadcasts.setdefault(channel.id, {})[broadcast.broadcast_id] = broadcast
        self.created.append(FakeCall(channel.id, broadcast.broadcast_id, spec.marker, None))
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
        spec: BroadcastSpec,
        category_id: str | None = None,
    ) -> None:
        if broadcast_id in self.fail_update:
            raise self.fail_update[broadcast_id]
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.id, {}).get(broadcast_id)
        if current is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.id}")
        self._broadcasts[channel.id][broadcast_id] = replace(
            current,
            title=spec.title,
            description=spec.description,
        )
        self.updated.append(FakeCall(channel.id, broadcast_id, spec.marker, None))

    def attach_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        if broadcast_id in self.fail_attach:
            raise self.fail_attach[broadcast_id]
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.id, {}).get(broadcast_id)
        if current is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.id}")
        number: int = self._next_number()
        stream: StreamInfo = StreamInfo(
            stream_id=FAKE_STREAM_ID_TEMPLATE.format(number=number),
            title=spec.marker,
            ingestion_address=FAKE_STREAM_URL,
            stream_name=FAKE_STREAM_KEY_TEMPLATE.format(number=number),
        )
        self._streams.setdefault(channel.id, {})[stream.stream_id] = stream
        self._broadcasts[channel.id][broadcast_id] = replace(current, stream_id=stream.stream_id)
        self.attached.append(FakeCall(channel.id, broadcast_id, spec.marker, None))
        return CreatedBroadcast(
            broadcast_id=broadcast_id,
            broadcast_url=broadcast_url_for(channel, broadcast_id),
            stream_id=stream.stream_id,
            stream_url=stream.ingestion_address,
            stream_key=stream.stream_name,
        )

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
    ) -> VideoFixes:
        self.settings_calls.append(broadcast_id)
        if broadcast_id in self.fail_settings:
            raise self.fail_settings[broadcast_id]
        fixes: VideoFixes = VideoFixes(
            language_set=self.languages.get(broadcast_id) != language,
            category_set=self.categories.get(broadcast_id) != category_id,
            audience_cleared=self.made_for_kids.get(broadcast_id, False),
        )
        if not fixes.any_fix:
            return fixes
        self.languages[broadcast_id] = language
        self.categories[broadcast_id] = category_id
        self.made_for_kids[broadcast_id] = False
        self.settings_writes.append(broadcast_id)
        return fixes

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        if broadcast_id in self.fail_thumbnail:
            raise self.fail_thumbnail[broadcast_id]
        self.thumbnails.append(FakeCall(channel.id, broadcast_id, "", preview))

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.facts_calls.append(broadcast_id)
        if broadcast_id in self.fail_facts:
            raise self.fail_facts[broadcast_id]
        override: BroadcastFacts | None = self.facts_override.get(broadcast_id)
        if override is not None:
            return override
        broadcast: UpcomingBroadcast | None = self._broadcasts.get(channel.id, {}).get(broadcast_id)
        if broadcast is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.id}")
        stream: StreamInfo | None = (
            self._streams.get(channel.id, {}).get(broadcast.stream_id) if broadcast.stream_id else None
        )
        return BroadcastFacts(
            broadcast_id=broadcast_id,
            title=broadcast.title,
            description=broadcast.description,
            start_utc=broadcast.start_utc,
            privacy_status=channel.privacy.value,
            made_for_kids=self.made_for_kids.get(broadcast_id, False),
            age_restricted=broadcast_id in self.age_restricted,
            default_language=self.languages.get(broadcast_id),
            default_audio_language=self.languages.get(broadcast_id),
            category_id=self.categories.get(broadcast_id, broadcast.category_id),
            bound_stream_id=broadcast.stream_id,
            stream_marker=stream.title if stream is not None else None,
            live_chat_id=self.live_chat_ids.get(broadcast_id),
        )

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter
