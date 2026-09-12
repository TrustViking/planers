"""Площадка трансляций (ТЗ §7.3, §7.4): Protocol BroadcastPlatform и её данные.

Любой сбой площадки — только PlatformError; другие исключения площадка не выпускает.
Protocol называется BroadcastPlatform: имя Platform занято enum-ом в app/config/loader.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from app.config.loader import ChannelConfig, Platform
from app.package.model import Slot

# Ссылка на эфир по его id — единственный источник (у UpcomingBroadcast ссылки нет).
BROADCAST_URL_TEMPLATES: Final[dict[Platform, str]] = {
    Platform.YOUTUBE: "https://www.youtube.com/watch?v={broadcast_id}",
}


@dataclass(frozen=True)
class ChannelInfo:
    """Кто мы на площадке (ТЗ §5.3): проверка «токен ведёт на тот канал»."""

    youtube_channel_id: str
    title: str
    default_language: str | None   # язык канала на площадке; справочный, на решения не влияет


@dataclass(frozen=True)
class UpcomingBroadcast:
    broadcast_id: str
    start_utc: datetime      # aware, UTC
    title: str
    description: str
    stream_id: str | None    # привязанный поток; None — поток не привязан


@dataclass(frozen=True)
class StreamInfo:
    stream_id: str
    title: str               # маркер §7.3: планер пишет сюда slot_id
    ingestion_address: str   # stream_url
    stream_name: str         # ключ потока


@dataclass(frozen=True)
class CreatedBroadcast:
    broadcast_id: str
    broadcast_url: str
    stream_id: str
    stream_url: str
    stream_key: str


class PlatformError(Exception):
    """Сбой площадки: code — код площадки (liveStreamingNotEnabled), message — пояснение для отчёта."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code: str = code
        self.message: str = message


class BroadcastPlatform(Protocol):
    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """Канал, на который ведёт токен: id, название, язык канала (ТЗ §5.3)."""
        ...

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        """Запланированные эфиры канала (§7.3 п.1)."""
        ...

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        """Привязанный поток: маркер (title) и ключ; None — потока нет."""
        ...

    def create_broadcast(self, channel: ChannelConfig, slot: Slot, preview: bytes | None) -> CreatedBroadcast:
        """Все шаги §7.4 п.1–4; маркер — slot.slot_id в названии потока."""
        ...

    def update_broadcast(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        slot: Slot,
        preview: bytes | None,
    ) -> None:
        """Исправление на месте (§7.3): название, описание, превью; ключ и ссылка не меняются."""
        ...


def broadcast_url_for(channel: ChannelConfig, broadcast_id: str) -> str:
    return BROADCAST_URL_TEMPLATES[channel.platform].format(broadcast_id=broadcast_id)
