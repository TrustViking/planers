"""FakePlatform — площадка в памяти: только для тестов.

Хранение по channel.account_name (каждый канал — отдельный YouTube-канал). Вход в канал
имитируется: канал из tokens_missing при первом обращении вызывает on_login, как YouTube перед браузером. Идентификаторы
детерминированные (счётчик); ключи — 5 групп по 4 символа [a-z0-9], как у YouTube (§7.4).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Final

from app.config.loader import ChannelConfig
from app.pipeline.plan import BroadcastSpec
from app.platforms.base import (
    AppliedVideo,
    BroadcastFacts,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    PlatformNotice,
    PlatformNoticeKind,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
    PLACEHOLDER_TOKEN,
    broadcast_url_for,
    picture_sha,
)

FAKE_STREAM_URL: Final[str] = "rtmp://a.rtmp.youtube.com/live2"
FAKE_BROADCAST_ID_TEMPLATE: Final[str] = "fakebc{number:05d}"
FAKE_STREAM_ID_TEMPLATE: Final[str] = "fakestream{number:04d}"
FAKE_STREAM_KEY_TEMPLATE: Final[str] = "fake-{number:04d}-0000-0000-0000"
FAKE_CHANNEL_ID_TEMPLATE: Final[str] = "UCfake{account_name}"
FAKE_CHANNEL_TITLE_TEMPLATE: Final[str] = "{account_name}"  # название = account_name: проверка канала проходит
NOT_FOUND_CODE: Final[str] = "broadcastNotFound"
# Те же лимиты, что у YouTube: тесты должны ловить реальное поведение обрезки.
FAKE_TITLE_MAX_CHARS: Final[int] = 100
FAKE_DESCRIPTION_MAX_CHARS: Final[int] = 5000
FAKE_AUTO_STOP: Final[bool] = True
FAKE_LATENCY_PREFERENCE: Final[str] = "normal"
# Посеянный эфир по умолчанию выглядит так, как его поставил бы планер с настройками build_config.
SEED_PRIVACY: Final[str] = "public"
SEED_AUTO_START: Final[bool] = True
SEED_CATEGORY_ID: Final[str] = "22"


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
        self.fail_list: dict[str, PlatformError] = {}     # account_name → ошибка list_upcoming
        self.fail_create: dict[str, PlatformError] = {}   # slot_id → ошибка create_broadcast
        self.fail_describe: dict[str, PlatformError] = {}  # account_name → ошибка describe_channel
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
        self.markers_set: list[FakeCall] = []                 # set_stream_marker: (канал, поток, метка)
        self._notices: list[PlatformNotice] = []             # замечания площадки: seed_undated_broadcast
        self.fail_marker: dict[str, PlatformError] = {}      # stream_id → ошибка set_stream_marker
        self.languages: dict[str, str] = {}                # broadcast_id → записанный язык
        self.channel_info: dict[str, ChannelInfo] = {}     # account_name → ответ describe_channel
        self.describe_calls: list[str] = []
        self.tokens_missing: set[str] = set()              # account_name без токена: первый вызов — вход
        self.on_login: Callable[[ChannelConfig], None] | None = None
        self.logins: list[str] = []
        self.pictures: dict[str, str] = {}   # broadcast_id → отпечаток текущей картинки эфира

    def seed_broadcast(
        self,
        channel_id: str,
        start_utc: datetime,
        title: str,
        description: str,
        marker: str | None = None,
        stream_key: str | None = None,
        *,
        privacy: str = SEED_PRIVACY,
        auto_start: bool = SEED_AUTO_START,
        auto_stop: bool = FAKE_AUTO_STOP,
        latency_preference: str = FAKE_LATENCY_PREFERENCE,
        category_id: str = SEED_CATEGORY_ID,
        picture: str | None = None,
        stream_description: str = "",
    ) -> UpcomingBroadcast:
        """Эфир «уже на канале». marker — название потока (поток создаётся); без marker — эфир без потока.

        Категория, как у YouTube, лежит только у ресурса видео: в списке эфиров её нет.
        picture — отпечаток картинки эфира (None — картинка не скачалась, обложка не сверяется);
        stream_description — описание потока, в нём может быть отпечаток заглушки.
        """
        number: int = self._next_number()
        stream_id: str | None = None
        if marker is not None:
            stream_id = FAKE_STREAM_ID_TEMPLATE.format(number=number)
            self._streams.setdefault(channel_id, {})[stream_id] = StreamInfo(
                stream_id=stream_id,
                title=marker,
                ingestion_address=FAKE_STREAM_URL,
                stream_name=stream_key or FAKE_STREAM_KEY_TEMPLATE.format(number=number),
                description=stream_description,
            )
        broadcast: UpcomingBroadcast = UpcomingBroadcast(
            broadcast_id=FAKE_BROADCAST_ID_TEMPLATE.format(number=number),
            start_utc=start_utc.astimezone(timezone.utc),
            title=title,
            description=description,
            stream_id=stream_id,
            privacy_status=privacy,
            auto_start=auto_start,
            auto_stop=auto_stop,
            latency_preference=latency_preference,
        )
        self._broadcasts.setdefault(channel_id, {})[broadcast.broadcast_id] = broadcast
        self.categories[broadcast.broadcast_id] = category_id
        if picture is not None:
            self.pictures[broadcast.broadcast_id] = picture
        return broadcast

    @staticmethod
    def placeholder_of(account_name: str) -> str:
        """Заглушка обложки канала в фейке: своя у каждого канала, как у YouTube."""
        return picture_sha(account_name.encode("utf-8"))

    def seed_undated_broadcast(self, account_name: str, title: str) -> PlatformNotice:
        """Эфир без времени старта на канале: площадка его не отдаёт, а замечание копит до take_notices."""
        notice: PlatformNotice = PlatformNotice(PlatformNoticeKind.UNDATED_BROADCAST, account_name, title)
        self._notices.append(notice)
        return notice

    def take_notices(self) -> tuple[PlatformNotice, ...]:
        taken: tuple[PlatformNotice, ...] = tuple(self._notices)
        self._notices.clear()
        return taken

    def remove_broadcast(self, channel_id: str, broadcast_id: str) -> None:
        """Владелец удалил эфир руками."""
        self._broadcasts.get(channel_id, {}).pop(broadcast_id, None)

    @property
    def limits(self) -> PlatformLimits:
        return PlatformLimits(
            title_max_chars=FAKE_TITLE_MAX_CHARS,
            description_max_chars=FAKE_DESCRIPTION_MAX_CHARS,
            auto_stop=FAKE_AUTO_STOP,
            latency_preference=FAKE_LATENCY_PREFERENCE,
        )

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """По умолчанию — детерминированный ответ по channel.account_name; тест может задать свой."""
        self.describe_calls.append(channel.account_name)
        self._login_if_needed(channel)
        if channel.account_name in self.fail_describe:
            raise self.fail_describe[channel.account_name]
        return self.channel_info.get(channel.account_name, self.default_channel_info(channel.account_name))

    @staticmethod
    def default_channel_info(account_name: str) -> ChannelInfo:
        return ChannelInfo(
            youtube_channel_id=FAKE_CHANNEL_ID_TEMPLATE.format(account_name=account_name),
            title=FAKE_CHANNEL_TITLE_TEMPLATE.format(account_name=account_name),
            default_language=None,
        )

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        self.list_calls.append(channel.account_name)
        self._login_if_needed(channel)
        if channel.account_name in self.fail_list:
            raise self.fail_list[channel.account_name]
        broadcasts: list[UpcomingBroadcast] = [
            replace(broadcast, thumbnail_sha=self.pictures.get(broadcast.broadcast_id))
            for broadcast in self._broadcasts.get(channel.account_name, {}).values()
        ]
        return sorted(broadcasts, key=lambda broadcast: (broadcast.start_utc, broadcast.broadcast_id))

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        self.stream_calls.append((channel.account_name, stream_id))
        return self._streams.get(channel.account_name, {}).get(stream_id)

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        if spec.marker in self.fail_create:
            raise self.fail_create[spec.marker]
        number: int = self._next_number()
        # как у площадки: обложки ещё нет, картинка — заглушка канала, её отпечаток — в описании потока
        placeholder: str = self.placeholder_of(channel.account_name)
        stream: StreamInfo = StreamInfo(
            stream_id=FAKE_STREAM_ID_TEMPLATE.format(number=number),
            title=spec.marker,
            ingestion_address=FAKE_STREAM_URL,
            stream_name=FAKE_STREAM_KEY_TEMPLATE.format(number=number),
            description=PLACEHOLDER_TOKEN.format(sha=placeholder),
        )
        broadcast: UpcomingBroadcast = UpcomingBroadcast(
            broadcast_id=FAKE_BROADCAST_ID_TEMPLATE.format(number=number),
            start_utc=spec.start_minute,
            title=spec.title,
            description=spec.description,
            stream_id=stream.stream_id,
            privacy_status=spec.privacy,
            auto_start=spec.auto_start,
            auto_stop=spec.auto_stop,
            latency_preference=spec.latency_preference,
        )
        self._streams.setdefault(channel.account_name, {})[stream.stream_id] = stream
        self._broadcasts.setdefault(channel.account_name, {})[broadcast.broadcast_id] = broadcast
        self.pictures[broadcast.broadcast_id] = placeholder
        self.created.append(FakeCall(channel.account_name, broadcast.broadcast_id, spec.marker, None))
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
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.account_name, {}).get(broadcast_id)
        if current is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.account_name}")
        self._broadcasts[channel.account_name][broadcast_id] = replace(
            current,
            title=spec.title,
            description=spec.description,
        )
        self.updated.append(FakeCall(channel.account_name, broadcast_id, spec.marker, None))

    def attach_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        if broadcast_id in self.fail_attach:
            raise self.fail_attach[broadcast_id]
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.account_name, {}).get(broadcast_id)
        if current is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.account_name}")
        number: int = self._next_number()
        stream: StreamInfo = StreamInfo(
            stream_id=FAKE_STREAM_ID_TEMPLATE.format(number=number),
            title=spec.marker,
            ingestion_address=FAKE_STREAM_URL,
            stream_name=FAKE_STREAM_KEY_TEMPLATE.format(number=number),
        )
        self._streams.setdefault(channel.account_name, {})[stream.stream_id] = stream
        self._broadcasts[channel.account_name][broadcast_id] = replace(current, stream_id=stream.stream_id)
        self.attached.append(FakeCall(channel.account_name, broadcast_id, spec.marker, None))
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
        privacy: str,
    ) -> VideoFixes:
        self.settings_calls.append(broadcast_id)
        if broadcast_id in self.fail_settings:
            raise self.fail_settings[broadcast_id]
        current: UpcomingBroadcast | None = self._broadcasts.get(channel.account_name, {}).get(broadcast_id)
        fixes: VideoFixes = VideoFixes(
            language_set=self.languages.get(broadcast_id) != language,
            category_set=self.categories.get(broadcast_id) != category_id,
            audience_cleared=self.made_for_kids.get(broadcast_id, False),
            privacy_set=current is not None and current.privacy_status != privacy,
        )
        if not fixes.any_fix:
            return fixes
        self.languages[broadcast_id] = language
        self.categories[broadcast_id] = category_id
        self.made_for_kids[broadcast_id] = False
        if current is not None:
            self._broadcasts[channel.account_name][broadcast_id] = replace(current, privacy_status=privacy)
        self.settings_writes.append(broadcast_id)
        # как настоящая площадка: ответ записи говорит, что записано (VideoFixes.apply_to_facts)
        return replace(
            fixes,
            applied=AppliedVideo(
                language=language,
                audio_language=language,
                category_id=category_id,
                privacy=privacy,
                made_for_kids=False,
            ),
        )

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        if stream_id in self.fail_marker:
            raise self.fail_marker[stream_id]
        stream: StreamInfo | None = self._streams.get(channel.account_name, {}).get(stream_id)
        if stream is None:
            raise PlatformError(NOT_FOUND_CODE, f"stream {stream_id} not found on {channel.account_name}")
        self._streams[channel.account_name][stream_id] = replace(stream, title=marker)
        self.markers_set.append(FakeCall(channel.account_name, stream_id, marker, None))

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        if broadcast_id in self.fail_thumbnail:
            raise self.fail_thumbnail[broadcast_id]
        self.pictures[broadcast_id] = picture_sha(preview)
        self.thumbnails.append(FakeCall(channel.account_name, broadcast_id, "", preview))

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.facts_calls.append(broadcast_id)
        if broadcast_id in self.fail_facts:
            raise self.fail_facts[broadcast_id]
        override: BroadcastFacts | None = self.facts_override.get(broadcast_id)
        if override is not None:
            return override
        broadcast: UpcomingBroadcast | None = self._broadcasts.get(channel.account_name, {}).get(broadcast_id)
        if broadcast is None:
            raise PlatformError(NOT_FOUND_CODE, f"broadcast {broadcast_id} not found on {channel.account_name}")
        stream: StreamInfo | None = (
            self._streams.get(channel.account_name, {}).get(broadcast.stream_id) if broadcast.stream_id else None
        )
        return BroadcastFacts(
            broadcast_id=broadcast_id,
            title=broadcast.title,
            description=broadcast.description,
            start_utc=broadcast.start_utc,
            privacy_status=broadcast.privacy_status,
            made_for_kids=self.made_for_kids.get(broadcast_id, False),
            age_restricted=broadcast_id in self.age_restricted,
            default_language=self.languages.get(broadcast_id),
            default_audio_language=self.languages.get(broadcast_id),
            category_id=self.categories.get(broadcast_id, broadcast.category_id),
            bound_stream_id=broadcast.stream_id,
            stream_marker=stream.title if stream is not None else None,
            live_chat_id=self.live_chat_ids.get(broadcast_id),
            auto_start=broadcast.auto_start,
            auto_stop=broadcast.auto_stop,
            latency_preference=broadcast.latency_preference,
        )

    def _login_if_needed(self, channel: ChannelConfig) -> None:
        if channel.account_name not in self.tokens_missing:
            return
        self.tokens_missing.discard(channel.account_name)
        self.logins.append(channel.account_name)
        if self.on_login is not None:
            self.on_login(channel)

    def _next_number(self) -> int:
        self._counter += 1
        return self._counter
