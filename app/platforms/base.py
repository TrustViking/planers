"""Площадка трансляций (ТЗ §7.3, §7.4): Protocol BroadcastPlatform и её данные.

Любой сбой площадки — только PlatformError; другие исключения площадка не выпускает.
Protocol называется BroadcastPlatform: имя Platform занято enum-ом в app/config/loader.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Final, Protocol

from app.config.loader import ChannelConfig, Platform

if TYPE_CHECKING:   # спека живёт в pipeline; здесь она нужна только для аннотаций
    from app.pipeline.plan import BroadcastSpec

# Ссылка на эфир по его id — единственный источник (у UpcomingBroadcast ссылки нет).
BROADCAST_URL_TEMPLATES: Final[dict[Platform, str]] = {
    Platform.YOUTUBE: "https://www.youtube.com/watch?v={broadcast_id}",
}


@dataclass(frozen=True)
class PlatformLimits:
    """Что задаёт сама площадка: лимиты текстов эфира и постоянные настройки эфира планера на ней.

    Единственный источник — сама площадка (для YouTube — константы app/platforms/youtube.py):
    спека берёт их отсюда, чтобы отправляемое и сравниваемое совпадали по построению.
    """

    title_max_chars: int
    description_max_chars: int
    auto_stop: bool               # enableAutoStop: эфир завершается сам, когда поток пропал
    latency_preference: str       # latencyPreference


@dataclass(frozen=True)
class ChannelInfo:
    """Кто мы на площадке (ТЗ §5.3): проверка «токен ведёт на тот канал»."""

    youtube_channel_id: str
    title: str
    default_language: str | None   # язык канала на площадке; справочный, на решения не влияет


@dataclass(frozen=True)
class UpcomingBroadcast:
    broadcast_id: str
    start_utc: datetime        # aware, UTC
    title: str
    description: str
    stream_id: str | None      # привязанный поток; None — поток не привязан
    live_chat_id: str | None = None   # чат заведён; включением чата API не управляет
    category_id: str | None = None   # snippet.categoryId; None — площадка его в списке эфиров не вернула
    privacy_status: str | None = None       # status.privacyStatus
    auto_start: bool | None = None          # contentDetails.enableAutoStart
    auto_stop: bool | None = None           # contentDetails.enableAutoStop
    latency_preference: str | None = None   # contentDetails.latencyPreference


class PlatformNoticeKind(str, Enum):
    UNDATED_BROADCAST = "undated_broadcast"   # эфир без времени старта: площадка его отбрасывает


@dataclass(frozen=True)
class PlatformNotice:
    """Замечание площадки за запуск: не сбой и не эфир планера, но владелец должен о нём узнать."""

    kind: PlatformNoticeKind
    account_name: str
    title: str


@dataclass(frozen=True)
class StreamInfo:
    stream_id: str
    title: str               # маркер §7.3: планер пишет сюда slot_id
    ingestion_address: str   # stream_url
    stream_name: str         # ключ потока


@dataclass(frozen=True)
class BroadcastFacts:
    """Что по факту лежит на платформе после действий планера.

    Язык, аудитория и возрастное ограничение в ответе liveBroadcasts.list не приходят:
    их видно только у ресурса videos с тем же id. Без этого разбирать расхождения нечем.
    """

    broadcast_id: str
    title: str
    description: str
    start_utc: datetime | None
    privacy_status: str | None
    made_for_kids: bool | None
    age_restricted: bool          # ytRating == ytAgeRestricted; через API только читается
    default_language: str | None
    default_audio_language: str | None
    category_id: str | None
    bound_stream_id: str | None
    stream_marker: str | None
    live_chat_id: str | None = None
    thumbnail_url: str | None = None
    # у ресурса videos этих полей нет: они из liveBroadcasts.list того же эфира
    auto_start: bool | None = None
    auto_stop: bool | None = None
    latency_preference: str | None = None


@dataclass(frozen=True)
class VideoFixes:
    """Что пришлось поправить у ресурса видео: владелец должен знать о расхождении."""

    language_set: bool = False
    category_set: bool = False
    audience_cleared: bool = False
    privacy_set: bool = False

    @property
    def any_fix(self) -> bool:
        return self.language_set or self.category_set or self.audience_cleared or self.privacy_set


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
    @property
    def limits(self) -> PlatformLimits:
        """Пределы длины названия и описания (ТЗ §7.4 п.1)."""
        ...

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """Канал, на который ведёт токен: id, название, язык канала (ТЗ §5.3)."""
        ...

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        """Запланированные эфиры канала (§7.3 п.1)."""
        ...

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        """Привязанный поток: маркер (title) и ключ; None — потока нет."""
        ...

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        """Все шаги §7.4 п.1–4; маркер потока — spec.marker.

        Площадка получает готовую спеку, а не слот: отправляемое и сравниваемое
        обязаны совпадать по построению.
        """
        ...

    def update_broadcast(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> None:
        """Исправление на месте (§7.3): название, описание, время, категория.

        update заменяет snippet целиком, поэтому время и категория отправляются всегда;
        категория берётся из planer.json, а не та, что стояла у эфира.
        """
        ...

    def attach_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        """Эфир есть, потока нет: создать поток с маркером и привязать (§7.3, §7.4 п.2–3)."""
        ...

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
        privacy: str,
    ) -> VideoFixes:
        """Язык, категория, видимость и аудитория у ресурса videos — одним чтением и не более чем одной записью.

        Язык и аудиторию у liveBroadcast не записать; видимость и категорию ресурс videos держит
        сам. Совпало всё — записи не делается вовсе.
        """
        ...

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        """Метка планера на уже существующем потоке: название — slot_id, описание — чей это ключ."""
        ...

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        """Обложка эфира (§7.4 п.4). Сбой не отменяет эфир — решает вызывающий."""
        ...

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        """Что лежит на платформе: для разбора расхождений (§5.6)."""
        ...

    def take_notices(self) -> tuple[PlatformNotice, ...]:
        """Замечания, накопленные за запуск (эфир без времени старта); отдаёт и очищает накопитель."""
        ...


def broadcast_url_for(channel: ChannelConfig, broadcast_id: str) -> str:
    return BROADCAST_URL_TEMPLATES[channel.platform].format(broadcast_id=broadcast_id)
