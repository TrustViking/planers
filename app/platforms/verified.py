"""Площадка с проверкой канала при первом обращении (ТЗ §5.3).

Обёртка над любой BroadcastPlatform. Первое обращение к каналу за запуск — describe_channel:
у YouTube это же и вход (токена нет — браузер), затем проверка в порядке ник → id по паспорту → название:
  - у канала на YouTube нет ника — отказ channelHandleMissing;
  - ник другой: паспорт по нику из channels.json подтверждает тот же id — выровнять (ChannelSync) и работать,
    иначе — отказ channelHandleMismatch;
  - ник совпал, а в паспорте у этого ника другой id — отказ channelIdMismatch;
  - название другое при совпавшем нике — выровнять название и работать.
Выравнивание по ходу запуска меняет файлы (channels.json, токен, паспорт), но не этот запуск: в выводе
и форме остаются прежние значения. Отказ — ChannelBindingError: это PlatformError, и сверка изолирует его
как любой сбой канала (все объекты канала — ошибка, остальные каналы работают).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from app.config.loader import ChannelConfig
from app.core.text import handle_from_custom_url
from app.observability.logging_setup import get_logger
from app.platforms.base import (
    BroadcastFacts,
    BroadcastPlatform,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    PlatformNotice,
    PlatformNoticeKind,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
)
from app.platforms.channel_sync import ChannelSync, normalize_channel_title, youtube_handle_key
from app.platforms.passport import PassportEntry
from app.ui import messages_ru as msg

if TYPE_CHECKING:   # спека живёт в pipeline; здесь она нужна только для аннотаций
    from app.pipeline.plan import BroadcastSpec

LOGGER = get_logger("channel")

ERROR_CHANNEL_HANDLE_MISSING: Final[str] = "channelHandleMissing"
ERROR_CHANNEL_HANDLE_MISMATCH: Final[str] = "channelHandleMismatch"
ERROR_CHANNEL_ID_MISMATCH: Final[str] = "channelIdMismatch"


class ChannelBindingError(PlatformError):
    """Канал за токеном не тот: message — готовый текст для владельца."""


class ChannelListener(Protocol):
    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        """Канал проверен: ник и id подтверждены."""
        ...


class VerifiedPlatform:
    """Каждый метод площадки сначала проверяет канал; проверка — один раз на канал (channel.key) за запуск."""

    def __init__(
        self,
        platform: BroadcastPlatform,
        sync: ChannelSync,
        listener: ChannelListener | None = None,
    ) -> None:
        self._platform: BroadcastPlatform = platform
        self._sync: ChannelSync = sync
        self._listener: ChannelListener | None = listener
        self._verified: dict[str, ChannelInfo] = {}
        self._refused: dict[str, ChannelBindingError] = {}

    @property
    def limits(self) -> PlatformLimits:
        return self._platform.limits

    def verify(self, channel: ChannelConfig) -> ChannelInfo:
        """Вход и проверка; сбой площадки не запоминается, отказ — до конца запуска."""
        key: str = channel.key
        if key in self._verified:
            return self._verified[key]
        if key in self._refused:
            raise self._refused[key]
        info: ChannelInfo = self._platform.describe_channel(channel)
        try:
            is_recorded: bool = self._check(channel, info)
        except ChannelBindingError as error:
            self._refused[key] = error
            raise
        if not is_recorded:
            self._sync.confirm(channel, info)
        self._verified[key] = info
        if self._listener is not None:
            self._listener.on_channel_ready(channel, info)
        return info

    def _check(self, channel: ChannelConfig, info: ChannelInfo) -> bool:
        """Ник → id → название. True — выравнивание уже записало паспорт."""
        handle_key: str | None = youtube_handle_key(info)
        if handle_key is None:
            raise self._refusal(channel, info, ERROR_CHANNEL_HANDLE_MISSING, msg.AUTH_CHANNEL_HANDLE_MISSING)
        entry: PassportEntry | None = self._sync.passport.find_by_key(channel.key)
        if handle_key != channel.key:
            if entry is None or entry.youtube_channel_id != info.youtube_channel_id:
                raise self._refusal(channel, info, ERROR_CHANNEL_HANDLE_MISMATCH, msg.AUTH_CHANNEL_HANDLE_MISMATCH)
            return self._sync.align_in_run(channel, info)
        if entry is not None and entry.youtube_channel_id != info.youtube_channel_id:
            raise self._refusal(channel, info, ERROR_CHANNEL_ID_MISMATCH, msg.AUTH_CHANNEL_ID_MISMATCH, entry)
        if normalize_channel_title(info.title) != channel.account_name:
            return self._sync.align_in_run(channel, info)
        return False

    def describe_channel(self, channel: ChannelConfig, *, allow_login: bool = True) -> ChannelInfo:
        """Проверка всегда с входом: без него канал не проверить (сверка при старте ходит мимо обёртки)."""
        return self.verify(channel)

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        self.verify(channel)
        return self._platform.list_upcoming(channel)

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        self.verify(channel)
        return self._platform.get_stream(channel, stream_id)

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        self.verify(channel)
        return self._platform.create_broadcast(channel, spec)

    def update_broadcast(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> None:
        self.verify(channel)
        self._platform.update_broadcast(channel, broadcast_id, spec)

    def attach_stream(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> CreatedBroadcast:
        self.verify(channel)
        return self._platform.attach_stream(channel, broadcast_id, spec)

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
        privacy: str,
    ) -> VideoFixes:
        self.verify(channel)
        return self._platform.apply_video_settings(channel, broadcast_id, language, category_id, privacy)

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        self.verify(channel)
        self._platform.set_stream_marker(channel, stream_id, marker)

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        self.verify(channel)
        self._platform.set_thumbnail(channel, broadcast_id, preview)

    def take_notices(self) -> tuple[PlatformNotice, ...]:
        """Замечания обёрнутой площадки и предупреждения выравнивания каналов по ходу запуска."""
        channel_notices: tuple[PlatformNotice, ...] = tuple(
            PlatformNotice(PlatformNoticeKind.CHANNEL, account_name="", title="", text=text)
            for text in self._sync.take_warnings()
        )
        return self._platform.take_notices() + channel_notices

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.verify(channel)
        return self._platform.read_facts(channel, broadcast_id)

    def _refusal(
        self,
        channel: ChannelConfig,
        info: ChannelInfo,
        code: str,
        template: str,
        entry: PassportEntry | None = None,
    ) -> ChannelBindingError:
        youtube_handle: str = handle_from_custom_url(info.handle_raw) if info.handle_raw else msg.AUTH_YOUTUBE_HANDLE_MISSING
        passport_channel_id: str = entry.youtube_channel_id if entry is not None else ""
        LOGGER.error(
            'channel_refused code=%s channel="%s" handle=%s youtube_title="%s" handle_raw=%s '
            "youtube_channel_id=%s passport_channel_id=%s",
            code,
            channel.account_name,
            channel.handle,
            info.title,
            info.handle_raw or "-",
            info.youtube_channel_id,
            passport_channel_id or "-",
        )
        return ChannelBindingError(
            code,
            template.format(
                account_name=channel.account_name,
                handle=channel.handle,
                youtube_title=info.title,
                youtube_handle=youtube_handle,
                youtube_channel_id=info.youtube_channel_id,
                passport_channel_id=passport_channel_id,
                channels_file=self._sync.paths.channels_file,
                token_file=self._sync.token_file(channel),
            ),
        )
