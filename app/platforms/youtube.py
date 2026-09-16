"""YouTube Data API v3 (ТЗ §7.3, §7.4): чтение и планирование эфиров.

Чтение: describe_channel, list_upcoming, get_stream. Планирование: create_broadcast,
update_broadcast, attach_stream, apply_video_settings, set_stream_marker. Любой сбой наружу — только PlatformError.
Всё, что уходит в эфир, берётся из BroadcastSpec: отправляемое и сравниваемое совпадают по построению.

Все обращения к YouTube идут через YouTubePlatform._execute: пауза youtube_pause_seconds между
обращениями, повторы и память отказов. Что делать с отказом, решает одна таблица — REASON_BEHAVIORS
(и OPERATION_REASON_BEHAVIORS для пар «операция, причина»), функция _error_behavior.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.config.loader import ChannelConfig
from app.google.auth import AuthError, load_credentials, token_file_for
from app.core.dates import format_datetime_text
from app.observability.logging_setup import get_logger, mask_stream_key
from app.pipeline.plan import BroadcastSpec
from app.pipeline.reconciler import MarkerParts, split_marker
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
    broadcast_url_for,
)
from app.ui import messages_ru as msg

LOGGER = get_logger("youtube")

API_SERVICE_NAME: Final[str] = "youtube"
API_VERSION: Final[str] = "v3"
BROADCAST_STATUS_UPCOMING: Final[str] = "upcoming"
# Пределы YouTube на тексты эфира (ТЗ §7.4 п.1) — единственный источник.
YOUTUBE_TITLE_MAX_CHARS: Final[int] = 100
YOUTUBE_DESCRIPTION_MAX_CHARS: Final[int] = 5000
MAX_RESULTS: Final[int] = 50
CHANNEL_PARTS: Final[str] = "snippet,brandingSettings"
BROADCAST_PARTS: Final[str] = "snippet,contentDetails,status"
STREAM_PARTS: Final[str] = "snippet,cdn"
STREAM_UPDATE_PARTS: Final[str] = "snippet"   # snippet целиком: частичная часть затёрла бы описание
BROADCAST_INSERT_PARTS: Final[str] = "snippet,status,contentDetails"
BROADCAST_UPDATE_PARTS: Final[str] = "snippet"   # без contentDetails: он требует monitorStream
BIND_PARTS: Final[str] = "id,contentDetails"
VIDEO_SETTINGS_PARTS: Final[str] = "snippet,status"   # один проход: язык, категория, аудитория
VIDEO_FACTS_PARTS: Final[str] = "snippet,status,contentDetails,liveStreamingDetails"
AGE_RESTRICTED_RATING: Final[str] = "ytAgeRestricted"
# Постоянный эфир канала: заводит сама площадка, времени старта у него нет, удалить нельзя.
DEFAULT_BROADCAST_FLAG: Final[str] = "isDefaultBroadcast"
RFC3339_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
INGESTION_TYPE: Final[str] = "rtmp"
STREAM_RESOLUTION: Final[str] = "variable"
STREAM_FRAME_RATE: Final[str] = "variable"
LATENCY_PREFERENCE: Final[str] = "normal"
ENABLE_AUTO_STOP: Final[bool] = True
THUMBNAIL_MIME_TYPE: Final[str] = "image/jpeg"

# Формат ключа потока YouTube (ТЗ §7.4) — единственный источник.
YOUTUBE_STREAM_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]{4}(-[a-z0-9]{4}){3,4}$")

# Коды ошибок площадки, которые планер называет сам (ответа Google за ними нет).
ERROR_CHANNEL_NOT_FOUND: Final[str] = "channelNotFound"
ERROR_AUTH: Final[str] = "authFailed"
ERROR_TRANSPORT: Final[str] = "transportFailed"
ERROR_BAD_RESPONSE: Final[str] = "badResponse"
ERROR_UNEXPECTED_KEY: Final[str] = "unexpectedStreamKeyFormat"
ERROR_UNKNOWN: Final[str] = "unknown"

LOG_MISSING: Final[str] = "-"   # чего площадка не прислала: строка лога остаётся key=value

RETRY_MAX_ATTEMPTS: Final[int] = 4
RETRY_BASE_DELAY_SEC: Final[float] = 1.0
RETRY_MAX_DELAY_SEC: Final[float] = 8.0
HTTP_SERVER_ERROR_MIN: Final[int] = 500
HTTP_TOO_MANY_REQUESTS: Final[int] = 429


class ErrorBehavior(str, Enum):
    """Что делать с отказом YouTube. Решает только _error_behavior."""

    RETRY = "retry"          # повторить с паузой
    CALL = "call"            # не повторять; следующий такой же вызов — как обычно
    OPERATION = "operation"  # не повторять; эту операцию на этом канале до конца запуска не вызывать
    CHANNEL = "channel"      # не повторять; к этому каналу до конца запуска не обращаться
    PROJECT = "project"      # не повторять; к YouTube до конца запуска не обращаться


# Причины отказа (errors[0].reason в ответе Google) и коды самого планера — единственная таблица поведения.
REASON_BEHAVIORS: Final[dict[str, ErrorBehavior]] = {
    "backendError": ErrorBehavior.RETRY,
    "internalError": ErrorBehavior.RETRY,
    "rateLimitExceeded": ErrorBehavior.RETRY,
    "userRateLimitExceeded": ErrorBehavior.RETRY,
    "userRequestsExceedRateLimit": ErrorBehavior.RETRY,
    "uploadRateLimitExceeded": ErrorBehavior.OPERATION,
    "userBroadcastsExceedLimit": ErrorBehavior.OPERATION,
    "liveStreamingNotEnabled": ErrorBehavior.OPERATION,
    "livePermissionBlocked": ErrorBehavior.OPERATION,
    "insufficientLivePermissions": ErrorBehavior.OPERATION,
    "authError": ErrorBehavior.CHANNEL,
    "insufficientPermissions": ErrorBehavior.CHANNEL,
    "channelClosed": ErrorBehavior.CHANNEL,
    "channelSuspended": ErrorBehavior.CHANNEL,
    "authenticatedUserAccountClosed": ErrorBehavior.CHANNEL,
    "authenticatedUserAccountSuspended": ErrorBehavior.CHANNEL,
    "authenticatedUserNotChannel": ErrorBehavior.CHANNEL,
    ERROR_AUTH: ErrorBehavior.CHANNEL,          # после отказа входа браузер повторно не открывается
    "quotaExceeded": ErrorBehavior.PROJECT,
    "invalidImage": ErrorBehavior.CALL,
    "mediaBodyRequired": ErrorBehavior.CALL,
    "videoNotFound": ErrorBehavior.CALL,
    ERROR_BAD_RESPONSE: ErrorBehavior.CALL,
    ERROR_UNEXPECTED_KEY: ErrorBehavior.CALL,
    ERROR_CHANNEL_NOT_FOUND: ErrorBehavior.CALL,   # совпадает с причиной Google channelNotFound
}
# Пара (операция, причина) главнее причины: forbidden у обложки — канал не подтверждён, у прочих — разовый отказ.
OPERATION_REASON_BEHAVIORS: Final[dict[tuple[str, str], ErrorBehavior]] = {
    ("thumbnails.set", "forbidden"): ErrorBehavior.OPERATION,
}
# Ключ памяти отказов: (канал, операция); None — «любой».
RefusalKey = tuple[str | None, str | None]


def _error_behavior(operation: str, http_status: int | None, reason: str) -> ErrorBehavior:
    """Единственное место решения. Причины нет в таблицах — по HTTP-коду: 5xx и 429 повторяем, прочее — нет."""
    paired: ErrorBehavior | None = OPERATION_REASON_BEHAVIORS.get((operation, reason))
    if paired is not None:
        return paired
    known: ErrorBehavior | None = REASON_BEHAVIORS.get(reason)
    if known is not None:
        return known
    if http_status is not None and (http_status >= HTTP_SERVER_ERROR_MIN or http_status == HTTP_TOO_MANY_REQUESTS):
        return ErrorBehavior.RETRY
    return ErrorBehavior.CALL


def _retry_delay(attempt: int) -> float:
    """Нарастающая пауза перед повтором: 1, 2, 4 с, не больше RETRY_MAX_DELAY_SEC."""
    return min(RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SEC)


def _refusal_key(behavior: ErrorBehavior, account_name: str, operation: str) -> RefusalKey | None:
    if behavior is ErrorBehavior.OPERATION:
        return (account_name, operation)
    if behavior is ErrorBehavior.CHANNEL:
        return (account_name, None)
    if behavior is ErrorBehavior.PROJECT:
        return (None, None)
    return None


@dataclass(frozen=True)
class _Failure:
    """Отказ одной попытки: что поднять и как с ним поступить."""

    error: PlatformError
    behavior: ErrorBehavior
    http_status: int | None


class YouTubePlatform:
    """Клиент строится лениво и кешируется по channel.account_name: один токен — один канал.

    Настройки эфира площадка не хранит: всё, что уходит в эфир, приходит готовой спекой (§7.4).
    on_login вызывается ровно перед открытием браузера для входа в канал.
    request_pause_sec — youtube_pause_seconds из planer.json: наименьший промежуток между обращениями.
    """

    def __init__(
        self,
        client_secret_file: Path,
        secrets_dir: Path,
        *,
        request_pause_sec: int,
        on_login: Callable[[ChannelConfig], None] | None = None,
    ) -> None:
        self._client_secret_file: Path = client_secret_file
        self._secrets_dir: Path = secrets_dir
        self._request_pause_sec: int = request_pause_sec
        self._on_login: Callable[[ChannelConfig], None] | None = on_login
        self._last_request_at: float | None = None   # time.monotonic() конца предыдущего обращения
        self._refusals: dict[RefusalKey, tuple[PlatformError, ErrorBehavior]] = {}
        self._services: dict[str, Any] = {}
        self._channels: dict[str, ChannelInfo] = {}
        self._notices: list[PlatformNotice] = []   # замечания за запуск; забирает take_notices

    @property
    def limits(self) -> PlatformLimits:
        return PlatformLimits(
            title_max_chars=YOUTUBE_TITLE_MAX_CHARS,
            description_max_chars=YOUTUBE_DESCRIPTION_MAX_CHARS,
            auto_stop=ENABLE_AUTO_STOP,
            latency_preference=LATENCY_PREFERENCE,
        )

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """Кеш на процесс: за запуск канал спрашивается один раз (квота §6.1 п.3)."""
        cached: ChannelInfo | None = self._channels.get(channel.account_name)
        if cached is not None:
            return cached
        response: dict[str, Any] = self._execute(
            channel,
            "channels.list",
            lambda service: service.channels().list(part=CHANNEL_PARTS, mine=True),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(
                ERROR_CHANNEL_NOT_FOUND,
                f"channels.list(mine=true) is empty for {channel.account_name}",
            )
        item: dict[str, Any] = items[0]
        snippet: dict[str, Any] = _mapping(item, "snippet")
        info: ChannelInfo = ChannelInfo(
            youtube_channel_id=_text(item, "id"),
            title=_text(snippet, "title"),
            default_language=_channel_language(item, snippet),
        )
        LOGGER.info(
            'channel_described channel="%s" youtube_channel_id=%s language=%s',
            channel.account_name,
            info.youtube_channel_id,
            info.default_language or LOG_MISSING,
        )
        self._channels[channel.account_name] = info
        return info

    def list_upcoming(self, channel: ChannelConfig) -> list[UpcomingBroadcast]:
        """Все страницы; пустой список — нормальный ответ, а не ошибка.

        broadcastStatus идёт БЕЗ mine: у liveBroadcasts.list фильтры id / mine /
        broadcastStatus взаимоисключающие (400 incompatibleParameters), а
        broadcastStatus сам по себе означает эфиры авторизованного пользователя.
        """
        broadcasts: list[UpcomingBroadcast] = []
        page_token: str | None = None
        while True:
            response: dict[str, Any] = self._execute(
                channel,
                "liveBroadcasts.list",
                lambda service, token=page_token: service.liveBroadcasts().list(
                    part=BROADCAST_PARTS,
                    broadcastStatus=BROADCAST_STATUS_UPCOMING,
                    maxResults=MAX_RESULTS,
                    pageToken=token,
                ),
            )
            page_broadcasts, page_notices = _broadcasts_from_page(_items(response), channel.account_name)
            broadcasts.extend(page_broadcasts)
            self._notices.extend(page_notices)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        LOGGER.info('broadcasts_listed channel="%s" count=%d', channel.account_name, len(broadcasts))
        return broadcasts

    def take_notices(self) -> tuple[PlatformNotice, ...]:
        """Отдать накопленные замечания и очистить накопитель."""
        taken: tuple[PlatformNotice, ...] = tuple(self._notices)
        self._notices.clear()
        return taken

    def get_stream(self, channel: ChannelConfig, stream_id: str) -> StreamInfo | None:
        response: dict[str, Any] = self._execute(
            channel,
            "liveStreams.list",
            lambda service: service.liveStreams().list(part=STREAM_PARTS, id=stream_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            return None
        item: dict[str, Any] = items[0]
        ingestion: dict[str, Any] = _mapping(_mapping(item, "cdn"), "ingestionInfo")
        stream: StreamInfo = StreamInfo(
            stream_id=_text(item, "id"),
            title=_text(_mapping(item, "snippet"), "title", allow_empty=True),
            ingestion_address=_text(ingestion, "ingestionAddress", allow_empty=True),
            stream_name=_text(ingestion, "streamName", allow_empty=True),
        )
        _warn_on_unexpected_key(channel.account_name, stream)
        return stream

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        """§7.4: insert эфира → insert потока → bind. Оборвалось — доделает следующий запуск."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveBroadcasts.insert",
            lambda service: service.liveBroadcasts().insert(
                part=BROADCAST_INSERT_PARTS,
                body=_broadcast_body(spec),
            ),
        )
        broadcast_id: str = _text(response, "id")
        LOGGER.info('broadcast_inserted channel="%s" broadcast_id=%s', channel.account_name, broadcast_id)
        return self._attach_new_stream(channel, broadcast_id, spec)

    def attach_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        """Эфир уже есть, потока нет: тот же путь, начиная с liveStreams.insert."""
        return self._attach_new_stream(channel, broadcast_id, spec)

    def update_broadcast(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> None:
        """update заменяет часть целиком: время и категорию из спеки отправляем всегда."""
        snippet: dict[str, Any] = {
            "title": spec.title,
            "description": spec.description,
            "scheduledStartTime": _rfc3339(spec.start_minute),
            "categoryId": spec.category_id,
        }
        self._execute(
            channel,
            "liveBroadcasts.update",
            lambda service: service.liveBroadcasts().update(
                part=BROADCAST_UPDATE_PARTS,
                body={"id": broadcast_id, "snippet": snippet},
            ),
        )
        LOGGER.info('broadcast_updated channel="%s" broadcast_id=%s', channel.account_name, broadcast_id)

    def apply_video_settings(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        language: str,
        category_id: str,
        privacy: str,
    ) -> VideoFixes:
        """Одно чтение и не более одной записи: язык и аудиторию у liveBroadcast не записать (§7.4).

        Части snippet и status отправляются целиком: частичная часть затирает
        непереданные поля. Видимость — отсюда же: status читается и пишется целиком.
        """
        item: dict[str, Any] = self._video_item(channel, broadcast_id, VIDEO_SETTINGS_PARTS)
        snippet: dict[str, Any] = dict(_mapping(item, "snippet"))
        status: dict[str, Any] = dict(_mapping(item, "status"))
        fixes: VideoFixes = VideoFixes(
            language_set=snippet.get("defaultLanguage") != language
            or snippet.get("defaultAudioLanguage") != language,
            category_set=snippet.get("categoryId") != category_id,
            audience_cleared=status.get("selfDeclaredMadeForKids") is not False
            or status.get("madeForKids") is True,
            privacy_set=status.get("privacyStatus") != privacy,
        )
        if not fixes.any_fix:
            return fixes
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
        snippet["categoryId"] = category_id
        status["selfDeclaredMadeForKids"] = False
        status["privacyStatus"] = privacy
        updated: dict[str, Any] = self._execute(
            channel,
            "videos.update",
            lambda service: service.videos().update(
                part=VIDEO_SETTINGS_PARTS,
                body={"id": broadcast_id, "snippet": snippet, "status": status},
            ),
        )
        applied: AppliedVideo = _applied_video(updated)
        LOGGER.info(
            'video_settings_applied channel="%s" broadcast_id=%s language=%s category=%s audience=%s privacy=%s'
            ' applied_language=%s applied_category=%s applied_privacy=%s',
            channel.account_name,
            broadcast_id,
            fixes.language_set,
            fixes.category_set,
            fixes.audience_cleared,
            fixes.privacy_set,
            applied.language or LOG_MISSING,
            applied.category_id or LOG_MISSING,
            applied.privacy or LOG_MISSING,
        )
        return replace(fixes, applied=applied)

    def set_stream_marker(self, channel: ChannelConfig, stream_id: str, marker: str) -> None:
        """liveStreams.update(part=snippet): snippet читается целиком, меняются только название и описание."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveStreams.list",
            lambda service: service.liveStreams().list(part=STREAM_UPDATE_PARTS, id=stream_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(ERROR_CHANNEL_NOT_FOUND, f"liveStreams.list is empty for {stream_id}")
        snippet: dict[str, Any] = dict(_mapping(items[0], "snippet"))
        snippet["title"] = marker
        snippet["description"] = stream_description(channel, marker, _local_now())
        self._execute(
            channel,
            "liveStreams.update",
            lambda service: service.liveStreams().update(
                part=STREAM_UPDATE_PARTS,
                body={"id": stream_id, "snippet": snippet},
            ),
        )
        LOGGER.info('stream_marker_set channel="%s" stream_id=%s marker=%s', channel.account_name, stream_id, marker)

    def read_facts(self, channel: ChannelConfig, broadcast_id: str) -> BroadcastFacts:
        """Что по факту лежит на платформе: язык, аудитория и возраст видны только у videos."""
        response: dict[str, Any] = self._execute(
            channel,
            "videos.list",
            lambda service: service.videos().list(part=VIDEO_FACTS_PARTS, id=broadcast_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(ERROR_CHANNEL_NOT_FOUND, f"videos.list is empty for {broadcast_id}")
        item: dict[str, Any] = items[0]
        snippet: dict[str, Any] = _mapping(item, "snippet")
        status: dict[str, Any] = _mapping(item, "status")
        rating: dict[str, Any] = _mapping(_mapping(item, "contentDetails"), "contentRating")
        # запланированное время у ресурса videos лежит в liveStreamingDetails, не в snippet
        live_details: dict[str, Any] = _mapping(item, "liveStreamingDetails")
        broadcast: UpcomingBroadcast | None = self._broadcast_of(channel, broadcast_id)
        stream_id: str | None = broadcast.stream_id if broadcast else None
        stream: StreamInfo | None = self.get_stream(channel, stream_id) if stream_id else None
        return BroadcastFacts(
            broadcast_id=broadcast_id,
            title=_text(snippet, "title", allow_empty=True),
            description=_text(snippet, "description", allow_empty=True),
            start_utc=_parse_start(live_details.get("scheduledStartTime")),
            privacy_status=_optional_text(status, "privacyStatus"),
            made_for_kids=_optional_bool(status, "madeForKids"),
            age_restricted=rating.get("ytRating") == AGE_RESTRICTED_RATING,
            default_language=_optional_text(snippet, "defaultLanguage"),
            default_audio_language=_optional_text(snippet, "defaultAudioLanguage"),
            category_id=_optional_text(snippet, "categoryId"),
            bound_stream_id=stream_id,
            stream_marker=stream.title if stream is not None else None,
            live_chat_id=broadcast.live_chat_id if broadcast else None,
            thumbnail_url=_largest_thumbnail(_mapping(snippet, "thumbnails")),
            auto_start=broadcast.auto_start if broadcast else None,
            auto_stop=broadcast.auto_stop if broadcast else None,
            latency_preference=broadcast.latency_preference if broadcast else None,
        )

    def _broadcast_of(self, channel: ChannelConfig, broadcast_id: str) -> UpcomingBroadcast | None:
        """Поток и чат: у videos их нет, спрашиваем сам эфир."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveBroadcasts.list",
            lambda service: service.liveBroadcasts().list(part=BROADCAST_PARTS, id=broadcast_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            return None
        parsed: UpcomingBroadcast | PlatformNotice | None = _broadcast_from_item(items[0], channel.account_name)
        return parsed if isinstance(parsed, UpcomingBroadcast) else None

    def _video_item(self, channel: ChannelConfig, broadcast_id: str, part: str) -> dict[str, Any]:
        """videos.list по id эфира: read-modify-write без чтения невозможен."""
        response: dict[str, Any] = self._execute(
            channel,
            "videos.list",
            lambda service: service.videos().list(part=part, id=broadcast_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(ERROR_CHANNEL_NOT_FOUND, f"videos.list is empty for {broadcast_id}")
        return items[0]

    def _attach_new_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        """liveStreams.insert → проверка ключа → bind. Один поток на эфир (§7.4)."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveStreams.insert",
            lambda service: service.liveStreams().insert(
                part=STREAM_PARTS,
                body=_stream_body(channel, spec),
            ),
        )
        stream_id: str = _text(response, "id")
        ingestion: dict[str, Any] = _mapping(_mapping(response, "cdn"), "ingestionInfo")
        stream_key: str = _text(ingestion, "streamName")
        if not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream_key):
            LOGGER.error(
                'stream_key_rejected channel="%s" stream_id=%s stream_key=%s',
                channel.account_name,
                stream_id,
                mask_stream_key(stream_key),
            )
            raise PlatformError(ERROR_UNEXPECTED_KEY, mask_stream_key(stream_key))
        self._execute(
            channel,
            "liveBroadcasts.bind",
            lambda service: service.liveBroadcasts().bind(
                part=BIND_PARTS,
                id=broadcast_id,
                streamId=stream_id,
            ),
        )
        LOGGER.info(
            'stream_bound channel="%s" broadcast_id=%s stream_id=%s stream_key=%s',
            channel.account_name,
            broadcast_id,
            stream_id,
            mask_stream_key(stream_key),
        )
        return CreatedBroadcast(
            broadcast_id=broadcast_id,
            broadcast_url=broadcast_url_for(channel, broadcast_id),
            stream_id=stream_id,
            stream_url=_text(ingestion, "ingestionAddress"),
            stream_key=stream_key,
        )

    def set_thumbnail(self, channel: ChannelConfig, broadcast_id: str, preview: bytes) -> None:
        """Превью не критично (ТЗ §7.4 п.4): сбой поднимается как PlatformError, решает вызывающий."""
        media: MediaInMemoryUpload = MediaInMemoryUpload(preview, mimetype=THUMBNAIL_MIME_TYPE)
        self._execute(
            channel,
            "thumbnails.set",
            lambda service: service.thumbnails().set(videoId=broadcast_id, media_body=media),
        )
        LOGGER.info('thumbnail_set channel="%s" broadcast_id=%s', channel.account_name, broadcast_id)

    def _service(self, channel: ChannelConfig) -> Any:
        cached: Any = self._services.get(channel.account_name)
        if cached is not None:
            return cached
        try:
            credentials: Any = load_credentials(
                self._client_secret_file,
                token_file_for(self._secrets_dir, channel.account_name),
                login_hint=channel.google_account,
                on_login=self._login_callback(channel),
            )
        except AuthError as error:
            raise PlatformError(ERROR_AUTH, f"{error.reason.value}: {error.detail}") from error
        service: Any = build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)
        self._services[channel.account_name] = service
        return service

    def _login_callback(self, channel: ChannelConfig) -> Callable[[], None] | None:
        if self._on_login is None:
            return None
        on_login: Callable[[ChannelConfig], None] = self._on_login
        return lambda: on_login(channel)

    def _execute(self, channel: ChannelConfig, operation: str, request_builder: Any) -> dict[str, Any]:
        """Единственная точка обращения к API: память отказов, пауза, повторы по _error_behavior."""
        self._raise_if_refused(channel, operation)
        service: Any = self._channel_service(channel, operation)
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            self._wait_pause()
            try:
                response: Any = request_builder(service).execute()
            except HttpError as error:
                failure: _Failure = _http_failure(operation, error)
            except OSError as error:
                failure = _Failure(
                    PlatformError(ERROR_TRANSPORT, f"{operation} on {channel.account_name}: {error}"),
                    ErrorBehavior.RETRY,
                    None,
                )
            else:
                self._mark_request_done()
                if not isinstance(response, dict):
                    raise PlatformError(ERROR_BAD_RESPONSE, f"{operation} returned {type(response).__name__}")
                return response
            self._mark_request_done()
            if failure.behavior is ErrorBehavior.RETRY and attempt < RETRY_MAX_ATTEMPTS:
                _sleep_before_retry(channel, operation, attempt, failure)
                continue
            raise self._refuse(channel, operation, failure)
        raise PlatformError(ERROR_TRANSPORT, f"{operation} on {channel.account_name}: retries exhausted")

    def _wait_pause(self) -> None:
        """Выждать остаток request_pause_sec от конца предыдущего обращения; паузы повторов входят в него."""
        if self._request_pause_sec <= 0 or self._last_request_at is None:
            return
        remaining: float = self._request_pause_sec - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _mark_request_done(self) -> None:
        self._last_request_at = time.monotonic()

    def _channel_service(self, channel: ChannelConfig, operation: str) -> Any:
        """Клиент канала; отказ входа — такой же отказ, как у запроса (ERROR_AUTH → CHANNEL)."""
        try:
            return self._service(channel)
        except PlatformError as error:
            behavior: ErrorBehavior = _error_behavior(operation, None, error.code)
            raise self._refuse(channel, operation, _Failure(error, behavior, None)) from error

    def _raise_if_refused(self, channel: ChannelConfig, operation: str) -> None:
        """Запомненный отказ поднимается без обращения к сети и без паузы."""
        for key in ((None, None), (channel.account_name, None), (channel.account_name, operation)):
            remembered: tuple[PlatformError, ErrorBehavior] | None = self._refusals.get(key)
            if remembered is None:
                continue
            error, behavior = remembered
            LOGGER.info(
                'request_skipped operation=%s channel="%s" reason=%s behavior=%s',
                operation,
                channel.account_name,
                error.code,
                behavior.value,
            )
            raise error

    def _refuse(self, channel: ChannelConfig, operation: str, failure: _Failure) -> PlatformError:
        """Окончательный отказ: запомнить по поведению и вернуть ошибку для raise."""
        error: PlatformError = failure.error
        is_server_side: bool = failure.http_status is None or failure.http_status >= HTTP_SERVER_ERROR_MIN
        if failure.behavior is ErrorBehavior.RETRY and is_server_side:
            # 5xx и сеть после всех попыток — «YouTube недоступен»; лимит частоты сохраняет свою причину
            error = PlatformError(ERROR_TRANSPORT, error.message)
        LOGGER.warning(
            'youtube_refused operation=%s channel="%s" http_status=%s reason=%s behavior=%s message="%s"',
            operation,
            channel.account_name,
            failure.http_status if failure.http_status is not None else LOG_MISSING,
            error.code,
            failure.behavior.value,
            error.message,
        )
        key: RefusalKey | None = _refusal_key(failure.behavior, channel.account_name, operation)
        if key is not None:
            self._refusals[key] = (error, failure.behavior)
        return error



def _sleep_before_retry(channel: ChannelConfig, operation: str, attempt: int, failure: _Failure) -> None:
    delay_sec: float = _retry_delay(attempt)
    LOGGER.warning(
        'request_retry operation=%s channel="%s" attempt=%d/%d delay_sec=%.1f http_status=%s reason=%s',
        operation,
        channel.account_name,
        attempt,
        RETRY_MAX_ATTEMPTS,
        delay_sec,
        failure.http_status if failure.http_status is not None else LOG_MISSING,
        failure.error.code,
    )
    time.sleep(delay_sec)   # через модуль time: тесты подменяют


def _largest_thumbnail(thumbnails: dict[str, Any]) -> str | None:
    """Самое крупное доступное разрешение; поле справочное, в сравнении не участвует."""
    best_url: str | None = None
    best_width: int = -1
    for value in thumbnails.values():
        if not isinstance(value, dict):
            continue
        url: Any = value.get("url")
        width: Any = value.get("width")
        if not isinstance(url, str) or not url:
            continue
        current: int = width if isinstance(width, int) else 0
        if current > best_width:
            best_url, best_width = url, current
    return best_url


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    value: Any = raw.get(key)
    return value if isinstance(value, str) and value else None


def _optional_bool(raw: dict[str, Any], key: str) -> bool | None:
    value: Any = raw.get(key)
    return value if isinstance(value, bool) else None


def _rfc3339(value: datetime) -> str:
    """Момент старта в том виде, в каком его ждёт YouTube (ТЗ §7.4 п.1)."""
    return value.astimezone(timezone.utc).strftime(RFC3339_FORMAT)


def _broadcast_body(spec: BroadcastSpec) -> dict[str, Any]:
    """Все значения — из спеки: там же они сверяются с тем, что лежит на площадке."""
    return {
        "snippet": {
            "title": spec.title,
            "description": spec.description,
            "scheduledStartTime": _rfc3339(spec.start_minute),
            "categoryId": spec.category_id,
        },
        "status": {
            "privacyStatus": spec.privacy,
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": spec.auto_start,
            "enableAutoStop": spec.auto_stop,
            "latencyPreference": spec.latency_preference,
        },
    }


def _local_now() -> datetime:
    """Момент записи метки — местным временем владельца, как все даты планера."""
    return datetime.now(timezone.utc).astimezone()


def stream_description(channel: ChannelConfig, marker: str, written_at: datetime) -> str:
    """Описание ключа потока в Студии: чей это ключ и для какого эфира. Зрителям не видно."""
    parts: MarkerParts | None = split_marker(marker)
    return msg.STREAM_DESCRIPTION.format(
        account_name=channel.account_name,
        date=parts.date if parts else marker,
        time=parts.time if parts else "",
        language=parts.language if parts else "",
        written_at=format_datetime_text(written_at),
    )


def _stream_body(channel: ChannelConfig, spec: BroadcastSpec) -> dict[str, Any]:
    """Название потока — маркер планера (§7.3), описание — чей это ключ; зрителям не видны."""
    return {
        "snippet": {
            "title": spec.marker,
            "description": stream_description(channel, spec.marker, _local_now()),
        },
        "cdn": {
            "ingestionType": INGESTION_TYPE,
            "resolution": STREAM_RESOLUTION,
            "frameRate": STREAM_FRAME_RATE,
        },
    }


def _items(response: dict[str, Any]) -> list[dict[str, Any]]:
    items: Any = response.get("items", [])
    if not isinstance(items, list):
        raise PlatformError(ERROR_BAD_RESPONSE, "items is not a list")
    return [item for item in items if isinstance(item, dict)]


def _applied_video(response: dict[str, Any]) -> AppliedVideo:
    """Ответ videos.update: что площадка записала на самом деле.

    Пустой ответ — пустой AppliedVideo: перечитанное значение тогда остаётся как есть.
    """
    snippet: dict[str, Any] = _mapping(response, "snippet")
    status: dict[str, Any] = _mapping(response, "status")
    made_for_kids: Any = status.get("madeForKids")
    return AppliedVideo(
        language=_optional_text(snippet, "defaultLanguage"),
        audio_language=_optional_text(snippet, "defaultAudioLanguage"),
        category_id=_optional_text(snippet, "categoryId"),
        privacy=_optional_text(status, "privacyStatus"),
        made_for_kids=made_for_kids if isinstance(made_for_kids, bool) else None,
    )


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value: Any = raw.get(key, {})
    if not isinstance(value, dict):
        raise PlatformError(ERROR_BAD_RESPONSE, f"{key} is not an object")
    return value


def _text(raw: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value: Any = raw.get(key, "")
    if not isinstance(value, str) or (not allow_empty and not value):
        raise PlatformError(ERROR_BAD_RESPONSE, f"{key}={value!r}")
    return value


def _channel_language(item: dict[str, Any], snippet: dict[str, Any]) -> str | None:
    """brandingSettings.channel.defaultLanguage, иначе snippet.defaultLanguage, иначе None."""
    branding: dict[str, Any] = _mapping(_mapping(item, "brandingSettings"), "channel")
    for source, key in ((branding, "defaultLanguage"), (snippet, "defaultLanguage")):
        value: Any = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _broadcasts_from_page(
    items: list[dict[str, Any]],
    account_name: str,
) -> tuple[list[UpcomingBroadcast], list[PlatformNotice]]:
    """Эфиры страницы и замечания о тех, что сверять не с чем."""
    broadcasts: list[UpcomingBroadcast] = []
    notices: list[PlatformNotice] = []
    for item in items:
        parsed: UpcomingBroadcast | PlatformNotice | None = _broadcast_from_item(item, account_name)
        if isinstance(parsed, UpcomingBroadcast):
            broadcasts.append(parsed)
        elif parsed is not None:
            notices.append(parsed)
    return broadcasts, notices


def _broadcast_from_item(item: dict[str, Any], account_name: str) -> UpcomingBroadcast | PlatformNotice | None:
    """Эфир без разбираемого времени старта сверять не с чем: вместо эфира — замечание для владельца.

    Постоянный эфир канала (isDefaultBroadcast) владелец не удалит и не исправит: без замечания, None.
    """
    snippet: dict[str, Any] = _mapping(item, "snippet")
    broadcast_id: str = _text(item, "id")
    start_text: Any = snippet.get("scheduledStartTime")
    start_utc: datetime | None = _parse_start(start_text)
    if start_utc is None and _optional_bool(snippet, DEFAULT_BROADCAST_FLAG):
        LOGGER.info(
            'default_broadcast_skipped channel="%s" broadcast_id=%s title=%r',
            account_name,
            broadcast_id,
            snippet.get("title"),
        )
        return None
    if start_utc is None:
        # лог — только диагностика; владельцу факт уходит данными (PlatformNotice → take_notices)
        LOGGER.info(
            'broadcast_without_start channel="%s" broadcast_id=%s title=%r value=%r',
            account_name,
            broadcast_id,
            snippet.get("title"),
            start_text,
        )
        return PlatformNotice(
            kind=PlatformNoticeKind.UNDATED_BROADCAST,
            account_name=account_name,
            title=str(snippet.get("title") or ""),
        )
    category_id: Any = snippet.get("categoryId")
    status: dict[str, Any] = _mapping(item, "status")
    details: dict[str, Any] = _mapping(item, "contentDetails")
    return UpcomingBroadcast(
        broadcast_id=broadcast_id,
        start_utc=start_utc,
        title=_text(snippet, "title", allow_empty=True),
        description=_text(snippet, "description", allow_empty=True),
        stream_id=_bound_stream_id(item),
        live_chat_id=_optional_text(snippet, "liveChatId"),
        category_id=category_id if isinstance(category_id, str) and category_id else None,
        privacy_status=_optional_text(status, "privacyStatus"),
        auto_start=_optional_bool(details, "enableAutoStart"),
        auto_stop=_optional_bool(details, "enableAutoStop"),
        latency_preference=_optional_text(details, "latencyPreference"),
    )


def _parse_start(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed: datetime = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bound_stream_id(item: dict[str, Any]) -> str | None:
    value: Any = _mapping(item, "contentDetails").get("boundStreamId")
    return value if isinstance(value, str) and value else None


def _warn_on_unexpected_key(account_name: str, stream: StreamInfo) -> None:
    """Ключ неожиданного вида не отбрасывается — только предупреждение (маска обязательна)."""
    if stream.stream_name and not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream.stream_name):
        LOGGER.warning(
            'stream_key_unexpected_format channel="%s" stream_id=%s stream_key=%s',
            account_name,
            stream.stream_id,
            mask_stream_key(stream.stream_name),
        )


def _http_failure(operation: str, error: HttpError) -> _Failure:
    status, reason, message = _google_error_details(error)
    return _Failure(PlatformError(reason, message), _error_behavior(operation, status, reason), status)


def _google_error_details(error: HttpError) -> tuple[int | None, str, str]:
    """HTTP-код, reason из тела ответа Google (liveStreamingNotEnabled и т.п.) и пояснение."""
    raw_status: Any = getattr(getattr(error, "resp", None), "status", None)
    status: int | None = _status_code(raw_status)
    payload: dict[str, Any] = _error_payload(error)
    body: dict[str, Any] = payload.get("error", {}) if isinstance(payload.get("error"), dict) else {}
    errors: Any = body.get("errors")
    reason: str = ERROR_UNKNOWN
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first: Any = errors[0].get("reason")
        if isinstance(first, str) and first:
            reason = first
    elif isinstance(body.get("status"), str) and body["status"]:
        reason = str(body["status"])
    message: Any = body.get("message")
    detail: str = message if isinstance(message, str) and message else str(error)
    return status, reason, f"HTTP {status}: {detail}" if status is not None else detail


def _status_code(value: Any) -> int | None:
    """resp.status у googleapiclient бывает и числом, и строкой."""
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _error_payload(error: HttpError) -> dict[str, Any]:
    content: Any = getattr(error, "content", None)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not isinstance(content, str) or not content:
        return {}
    try:
        payload: Any = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
