"""Площадка с проверкой канала при первом обращении (ТЗ §5.3).

Обёртка над любой BroadcastPlatform. Первое обращение к каналу за запуск — describe_channel:
у YouTube это же и вход (токена нет — браузер), затем сверка названия канала на YouTube
с account_name из channels.json (то же имя уходит в форму как «Название канала»).
Не совпало — ChannelBindingError: это PlatformError, и сверка изолирует его как любой сбой
канала (все объекты канала — ошибка, остальные каналы работают). Файлов планер тут не пишет.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from app.config.loader import ChannelConfig
from app.observability.logging_setup import get_logger
from app.platforms.base import (
    BroadcastFacts,
    BroadcastPlatform,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    PlatformNotice,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
)
from app.ui import messages_ru as msg

if TYPE_CHECKING:   # спека живёт в pipeline; здесь она нужна только для аннотаций
    from app.pipeline.plan import BroadcastSpec

LOGGER = get_logger("channel")

ERROR_CHANNEL_NAME_MISMATCH: Final[str] = "channelNameMismatch"
CHANNEL_TITLE_FORM: Final[str] = "NFC"   # так же приводится account_name в config/loader.py


class ChannelBindingError(PlatformError):
    """Канал за токеном не тот: message — готовый текст для владельца."""


class ChannelListener(Protocol):
    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        """Канал проверен: название на YouTube совпало с account_name."""
        ...


def normalize_channel_title(title: str) -> str:
    return unicodedata.normalize(CHANNEL_TITLE_FORM, title).strip()


class VerifiedPlatform:
    """Каждый метод площадки сначала проверяет канал; проверка — один раз на канал за запуск."""

    def __init__(
        self,
        platform: BroadcastPlatform,
        channels_file: Path,
        listener: ChannelListener | None = None,
    ) -> None:
        self._platform: BroadcastPlatform = platform
        self._channels_file: Path = channels_file   # только для подсказки владельцу
        self._listener: ChannelListener | None = listener
        self._verified: dict[str, ChannelInfo] = {}
        self._refused: dict[str, ChannelBindingError] = {}

    @property
    def limits(self) -> PlatformLimits:
        return self._platform.limits

    def verify(self, channel: ChannelConfig) -> ChannelInfo:
        """Вход и сверка названия; сбой площадки не запоминается, отказ — до конца запуска."""
        name: str = channel.account_name
        if name in self._verified:
            return self._verified[name]
        if name in self._refused:
            raise self._refused[name]
        info: ChannelInfo = self._platform.describe_channel(channel)
        if normalize_channel_title(info.title) != name:
            error: ChannelBindingError = self._refusal(channel, info)
            self._refused[name] = error
            raise error
        self._verified[name] = info
        if self._listener is not None:
            self._listener.on_channel_ready(channel, info)
        return info

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
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
        """Замечания копит обёрнутая площадка; проверка канала тут не нужна."""
        return self._platform.take_notices()

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.verify(channel)
        return self._platform.read_facts(channel, broadcast_id)

    def _refusal(self, channel: ChannelConfig, info: ChannelInfo) -> ChannelBindingError:
        LOGGER.error(
            'channel_name_mismatch channel="%s" youtube_title="%s" youtube_channel_id=%s',
            channel.account_name,
            info.title,
            info.youtube_channel_id,
        )
        return ChannelBindingError(
            ERROR_CHANNEL_NAME_MISMATCH,
            msg.AUTH_CHANNEL_NAME_MISMATCH.format(
                account_name=channel.account_name,
                youtube_title=info.title,
                channels_file=self._channels_file,
            ),
        )
