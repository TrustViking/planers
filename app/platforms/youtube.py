"""YouTube Data API v3 (ТЗ §7.3, §7.4): чтение и планирование эфиров.

Чтение: describe_channel, list_upcoming, get_stream. Планирование: create_broadcast,
update_broadcast, attach_stream, apply_video_settings, set_stream_marker. Любой сбой наружу — только PlatformError.
Всё, что уходит в эфир, берётся из BroadcastSpec: отправляемое и сравниваемое совпадают по построению.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.config.loader import ChannelConfig
from app.google.api_retry import GoogleApiRetryPolicy, execute_with_retry
from app.google.auth import AuthError, load_credentials, token_file_for
from app.core.dates import format_datetime_text
from app.observability.logging_setup import get_logger, mask_stream_key
from app.pipeline.plan import BroadcastSpec
from app.pipeline.reconciler import MarkerParts, split_marker
from app.platforms.base import (
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

RETRY_MAX_ATTEMPTS: Final[int] = 4
RETRY_BASE_DELAY_SEC: Final[float] = 1.0
RETRY_MAX_DELAY_SEC: Final[float] = 8.0
TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})
RETRY_LOG_PREFIX: Final[str] = "youtube"
RETRY_RESOURCE_LABEL: Final[str] = "channel"


def _transient_error(status_code: int, resource_id: str, error: Exception) -> Exception:
    return PlatformError(ERROR_TRANSPORT, f"HTTP {status_code} on {resource_id}: {error}")


RETRY_POLICY: Final[GoogleApiRetryPolicy] = GoogleApiRetryPolicy(
    max_retries=RETRY_MAX_ATTEMPTS,
    base_delay_sec=RETRY_BASE_DELAY_SEC,
    max_delay_sec=RETRY_MAX_DELAY_SEC,
    transient_status_codes=TRANSIENT_STATUS_CODES,
    connection_errors=(OSError,),
    log_prefix=RETRY_LOG_PREFIX,
    resource_label=RETRY_RESOURCE_LABEL,
    transient_error_factory=_transient_error,
)


class YouTubePlatform:
    """Клиент строится лениво и кешируется по channel.account_name: один токен — один канал.

    Настройки эфира площадка не хранит: всё, что уходит в эфир, приходит готовой спекой (§7.4).
    on_login вызывается ровно перед открытием браузера для входа в канал.
    """

    def __init__(
        self,
        client_secret_file: Path,
        secrets_dir: Path,
        on_login: Callable[[ChannelConfig], None] | None = None,
    ) -> None:
        self._client_secret_file: Path = client_secret_file
        self._secrets_dir: Path = secrets_dir
        self._on_login: Callable[[ChannelConfig], None] | None = on_login
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
            info.default_language or "-",
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
        self._execute(
            channel,
            "videos.update",
            lambda service: service.videos().update(
                part=VIDEO_SETTINGS_PARTS,
                body={"id": broadcast_id, "snippet": snippet, "status": status},
            ),
        )
        LOGGER.info(
            'video_settings_applied channel="%s" broadcast_id=%s language=%s category=%s audience=%s privacy=%s',
            channel.account_name,
            broadcast_id,
            fixes.language_set,
            fixes.category_set,
            fixes.audience_cleared,
            fixes.privacy_set,
        )
        return fixes

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
        parsed: UpcomingBroadcast | PlatformNotice = _broadcast_from_item(items[0], channel.account_name)
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
        """Временная недоступность площадки (5xx) — не повод ронять канал: повторяем."""
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                return self._execute_once(channel, operation, request_builder)
            except PlatformError as error:
                if error.code != ERROR_TRANSPORT or attempt >= RETRY_MAX_ATTEMPTS:
                    raise
                delay_sec: float = RETRY_POLICY.compute_delay(attempt)
                LOGGER.warning(
                    'request_retry operation=%s channel="%s" attempt=%d/%d delay_sec=%.1f code=%s',
                    operation,
                    channel.account_name,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    delay_sec,
                    error.code,
                )
                time.sleep(delay_sec)
        raise PlatformError(ERROR_TRANSPORT, f"{operation} on {channel.account_name}: retries exhausted")

    def _execute_once(self, channel: ChannelConfig, operation: str, request_builder: Any) -> dict[str, Any]:
        service: Any = self._service(channel)
        try:
            response: Any = execute_with_retry(
                policy=RETRY_POLICY,
                logger=LOGGER,
                operation_name=operation,
                resource_id=channel.account_name,
                request_callable=lambda: request_builder(service).execute(),
            )
        except HttpError as error:
            raise _platform_error_from_http(error) from error
        except OSError as error:
            raise PlatformError(ERROR_TRANSPORT, f"{operation} on {channel.account_name}: {error}") from error
        if not isinstance(response, dict):
            raise PlatformError(ERROR_BAD_RESPONSE, f"{operation} returned {type(response).__name__}")
        return response


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
        parsed: UpcomingBroadcast | PlatformNotice = _broadcast_from_item(item, account_name)
        if isinstance(parsed, UpcomingBroadcast):
            broadcasts.append(parsed)
        else:
            notices.append(parsed)
    return broadcasts, notices


def _broadcast_from_item(item: dict[str, Any], account_name: str) -> UpcomingBroadcast | PlatformNotice:
    """Эфир без разбираемого времени старта сверять не с чем: вместо эфира — замечание для владельца."""
    snippet: dict[str, Any] = _mapping(item, "snippet")
    broadcast_id: str = _text(item, "id")
    start_text: Any = snippet.get("scheduledStartTime")
    start_utc: datetime | None = _parse_start(start_text)
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


def _platform_error_from_http(error: HttpError) -> PlatformError:
    reason, message = _google_error_details(error)
    return PlatformError(reason, message)


def _google_error_details(error: HttpError) -> tuple[str, str]:
    """reason из тела ответа Google (liveStreamingNotEnabled и т.п.) + пояснение."""
    status: Any = getattr(getattr(error, "resp", None), "status", None)
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
    return reason, f"HTTP {status}: {detail}" if status is not None else detail


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
