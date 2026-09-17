"""Площадка, которая пускает к YouTube только подтверждённые каналы (ТЗ §5.3).

Обёртка над любой BroadcastPlatform. Сама не входит и не проверяет: вход и проверка канала — объект Channel
и фаза входов (app/platforms/channel.py). Канал не READY — каждый метод поднимает ошибку, сохранённую в
объекте: REFUSED — ChannelBindingError с готовым текстом, FAILED — сбой площадки или входа, NEEDS_LOGIN —
loginRequired. Это PlatformError, и сверка изолирует его как любой сбой канала (все объекты канала —
ошибка, остальные каналы работают).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.config.loader import ChannelConfig
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
from app.platforms.channel import Channel, ChannelBook

if TYPE_CHECKING:   # спека живёт в pipeline; здесь она нужна только для аннотаций
    from app.pipeline.plan import BroadcastSpec


class VerifiedPlatform:
    """Каждый метод площадки сначала спрашивает объект канала: READY — дальше, иначе — его ошибка."""

    def __init__(self, platform: BroadcastPlatform, book: ChannelBook) -> None:
        self._platform: BroadcastPlatform = platform
        self._book: ChannelBook = book

    @property
    def limits(self) -> PlatformLimits:
        return self._platform.limits

    def require_ready(self, channel: ChannelConfig) -> Channel:
        """Объект подтверждённого канала; иначе — ошибка, сохранённая в объекте."""
        found: Channel = self._book.channel(channel)
        error: PlatformError | None = found.access_error()
        if error is not None:
            raise error
        return found

    def describe_channel(self, channel: ChannelConfig, *, allow_login: bool = True) -> ChannelInfo:
        """Что прислал YouTube при проверке канала; второго обращения к площадке нет."""
        found: Channel = self.require_ready(channel)
        if found.info is None:
            return self._platform.describe_channel(channel, allow_login=False)
        return found.info

    def keep_login(self, channel: ChannelConfig) -> None:
        """Вход — дело ChannelBook: через шлюз токены не пишутся."""

    def drop_login(self, channel: ChannelConfig) -> None:
        """Вход — дело ChannelBook: через шлюз клиенты не забываются."""

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        self.require_ready(channel)
        return self._platform.list_upcoming(channel)

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        self.require_ready(channel)
        return self._platform.get_stream(channel, stream_id)

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        self.require_ready(channel)
        return self._platform.create_broadcast(channel, spec)

    def update_broadcast(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> None:
        self.require_ready(channel)
        self._platform.update_broadcast(channel, broadcast_id, spec)

    def attach_stream(self, channel: ChannelConfig, broadcast_id: str, spec: BroadcastSpec) -> CreatedBroadcast:
        self.require_ready(channel)
        return self._platform.attach_stream(channel, broadcast_id, spec)

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
        privacy: str,
    ) -> VideoFixes:
        self.require_ready(channel)
        return self._platform.apply_video_settings(channel, broadcast_id, language, category_id, privacy)

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        self.require_ready(channel)
        self._platform.set_stream_marker(channel, stream_id, marker)

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        self.require_ready(channel)
        self._platform.set_thumbnail(channel, broadcast_id, preview)

    def thumbnail_refusal(self, channel: ChannelConfig) -> PlatformError | None:
        """Канал не READY — к площадке по нему не ходим вовсе, отказа обложек знать неоткуда."""
        if self._book.channel(channel).access_error() is not None:
            return None
        return self._platform.thumbnail_refusal(channel)

    def take_notices(self) -> tuple[PlatformNotice, ...]:
        """Замечания обёрнутой площадки и предупреждения каналов, накопленные по ходу запуска (входы)."""
        channel_notices: tuple[PlatformNotice, ...] = tuple(
            PlatformNotice(PlatformNoticeKind.CHANNEL, account_name="", title="", text=text)
            for text in self._book.take_warnings()
        )
        return self._platform.take_notices() + channel_notices

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        self.require_ready(channel)
        return self._platform.read_facts(channel, broadcast_id)
