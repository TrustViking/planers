"""YouTube Data API v3 (ТЗ §7.3, §7.4): чтение и планирование эфиров.

Чтение: describe_channel, list_upcoming, get_stream. Планирование: create_broadcast,
update_broadcast, attach_stream, set_language. Любой сбой наружу — только PlatformError.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.config.loader import ChannelConfig
from app.google.api_retry import GoogleApiRetryPolicy, execute_with_retry
from app.google.auth import AuthError, load_credentials, token_file_for
from app.observability.logging_setup import get_logger, mask_stream_key
from app.pipeline.plan import BroadcastSpec
from app.platforms.base import (
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformLimits,
    StreamInfo,
    UpcomingBroadcast,
    broadcast_url_for,
)

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
BROADCAST_INSERT_PARTS: Final[str] = "snippet,status,contentDetails"
BROADCAST_UPDATE_PARTS: Final[str] = "snippet"   # без contentDetails: он требует monitorStream
BIND_PARTS: Final[str] = "id,contentDetails"
VIDEO_PARTS: Final[str] = "snippet"
RFC3339_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"
INGESTION_TYPE: Final[str] = "rtmp"
STREAM_RESOLUTION: Final[str] = "variable"
STREAM_FRAME_RATE: Final[str] = "variable"
LATENCY_PREFERENCE: Final[str] = "normal"
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
    """Клиент строится лениво и кешируется по channel.id: один токен — один канал."""

    def __init__(self, client_secret_file: Path, secrets_dir: Path) -> None:
        self._client_secret_file: Path = client_secret_file
        self._secrets_dir: Path = secrets_dir
        self._services: dict[str, Any] = {}
        self._channels: dict[str, ChannelInfo] = {}

    @property
    def limits(self) -> PlatformLimits:
        return PlatformLimits(
            title_max_chars=YOUTUBE_TITLE_MAX_CHARS,
            description_max_chars=YOUTUBE_DESCRIPTION_MAX_CHARS,
        )

    def describe_channel(self, channel: ChannelConfig) -> ChannelInfo:
        """Кеш на процесс: за запуск канал спрашивается один раз (квота §6.1 п.3)."""
        cached: ChannelInfo | None = self._channels.get(channel.id)
        if cached is not None:
            return cached
        response: dict[str, Any] = self._execute(
            channel,
            "channels.list",
            lambda service: service.channels().list(part=CHANNEL_PARTS, mine=True),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(ERROR_CHANNEL_NOT_FOUND, f"channels.list(mine=true) is empty for {channel.id}")
        item: dict[str, Any] = items[0]
        snippet: dict[str, Any] = _mapping(item, "snippet")
        info: ChannelInfo = ChannelInfo(
            youtube_channel_id=_text(item, "id"),
            title=_text(snippet, "title"),
            default_language=_channel_language(item, snippet),
        )
        LOGGER.info(
            "channel_described channel=%s youtube_channel_id=%s language=%s",
            channel.id,
            info.youtube_channel_id,
            info.default_language or "-",
        )
        self._channels[channel.id] = info
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
            broadcasts.extend(_broadcasts_from_page(_items(response), channel.id))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        LOGGER.info("broadcasts_listed channel=%s count=%d", channel.id, len(broadcasts))
        return broadcasts

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
        _warn_on_unexpected_key(channel.id, stream)
        return stream

    def create_broadcast(self, channel: ChannelConfig, spec: BroadcastSpec) -> CreatedBroadcast:
        """§7.4: insert эфира → insert потока → bind. Оборвалось — доделает следующий запуск."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveBroadcasts.insert",
            lambda service: service.liveBroadcasts().insert(
                part=BROADCAST_INSERT_PARTS,
                body=_broadcast_body(channel, spec),
            ),
        )
        broadcast_id: str = _text(response, "id")
        LOGGER.info("broadcast_inserted channel=%s broadcast_id=%s", channel.id, broadcast_id)
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
        category_id: str | None = None,
    ) -> None:
        """update заменяет переданную часть целиком: время и категорию отправляем всегда."""
        snippet: dict[str, Any] = {
            "title": spec.title,
            "description": spec.description,
            "scheduledStartTime": _rfc3339(spec.start_minute),
        }
        if category_id:
            snippet["categoryId"] = category_id
        self._execute(
            channel,
            "liveBroadcasts.update",
            lambda service: service.liveBroadcasts().update(
                part=BROADCAST_UPDATE_PARTS,
                body={"id": broadcast_id, "snippet": snippet},
            ),
        )
        LOGGER.info("broadcast_updated channel=%s broadcast_id=%s", channel.id, broadcast_id)

    def set_language(self, channel: ChannelConfig, broadcast_id: str, language: str) -> None:
        """Только read-modify-write: частичный snippet затирает непереданные поля."""
        response: dict[str, Any] = self._execute(
            channel,
            "videos.list",
            lambda service: service.videos().list(part=VIDEO_PARTS, id=broadcast_id),
        )
        items: list[dict[str, Any]] = _items(response)
        if not items:
            raise PlatformError(ERROR_CHANNEL_NOT_FOUND, f"videos.list is empty for {broadcast_id}")
        snippet: dict[str, Any] = dict(_mapping(items[0], "snippet"))
        snippet["defaultLanguage"] = language
        snippet["defaultAudioLanguage"] = language
        self._execute(
            channel,
            "videos.update",
            lambda service: service.videos().update(
                part=VIDEO_PARTS,
                body={"id": broadcast_id, "snippet": snippet},
            ),
        )
        LOGGER.info(
            "broadcast_language_set channel=%s broadcast_id=%s language=%s",
            channel.id,
            broadcast_id,
            language,
        )

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
                body=_stream_body(spec),
            ),
        )
        stream_id: str = _text(response, "id")
        ingestion: dict[str, Any] = _mapping(_mapping(response, "cdn"), "ingestionInfo")
        stream_key: str = _text(ingestion, "streamName")
        if not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream_key):
            LOGGER.error(
                "stream_key_rejected channel=%s stream_id=%s stream_key=%s",
                channel.id,
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
            "stream_bound channel=%s broadcast_id=%s stream_id=%s stream_key=%s",
            channel.id,
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
        LOGGER.info("thumbnail_set channel=%s broadcast_id=%s", channel.id, broadcast_id)

    def _service(self, channel: ChannelConfig) -> Any:
        cached: Any = self._services.get(channel.id)
        if cached is not None:
            return cached
        try:
            credentials: Any = load_credentials(
                self._client_secret_file,
                token_file_for(self._secrets_dir, channel.id),
            )
        except AuthError as error:
            raise PlatformError(ERROR_AUTH, f"{error.reason.value}: {error.detail}") from error
        service: Any = build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)
        self._services[channel.id] = service
        return service

    def _execute(self, channel: ChannelConfig, operation: str, request_builder: Any) -> dict[str, Any]:
        service: Any = self._service(channel)
        try:
            response: Any = execute_with_retry(
                policy=RETRY_POLICY,
                logger=LOGGER,
                operation_name=operation,
                resource_id=channel.id,
                request_callable=lambda: request_builder(service).execute(),
            )
        except HttpError as error:
            raise _platform_error_from_http(error) from error
        except OSError as error:
            raise PlatformError(ERROR_TRANSPORT, f"{operation} on {channel.id}: {error}") from error
        if not isinstance(response, dict):
            raise PlatformError(ERROR_BAD_RESPONSE, f"{operation} returned {type(response).__name__}")
        return response


def _rfc3339(value: datetime) -> str:
    """Момент старта в том виде, в каком его ждёт YouTube (ТЗ §7.4 п.1)."""
    return value.astimezone(timezone.utc).strftime(RFC3339_FORMAT)


def _broadcast_body(channel: ChannelConfig, spec: BroadcastSpec) -> dict[str, Any]:
    return {
        "snippet": {
            "title": spec.title,
            "description": spec.description,
            "scheduledStartTime": _rfc3339(spec.start_minute),
        },
        "status": {
            "privacyStatus": channel.privacy.value,
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": channel.auto_start,
            "enableAutoStop": True,
            "latencyPreference": LATENCY_PREFERENCE,
        },
    }


def _stream_body(spec: BroadcastSpec) -> dict[str, Any]:
    """Название потока — маркер планера (§7.3), зрителям он не виден."""
    return {
        "snippet": {"title": spec.marker},
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


def _broadcasts_from_page(items: list[dict[str, Any]], channel_key: str) -> list[UpcomingBroadcast]:
    broadcasts: list[UpcomingBroadcast] = []
    for item in items:
        broadcast: UpcomingBroadcast | None = _broadcast_from_item(item, channel_key)
        if broadcast is not None:
            broadcasts.append(broadcast)
    return broadcasts


def _broadcast_from_item(item: dict[str, Any], channel_key: str) -> UpcomingBroadcast | None:
    """Эфир без разбираемого времени старта пропускается: сверять его не с чем."""
    snippet: dict[str, Any] = _mapping(item, "snippet")
    broadcast_id: str = _text(item, "id")
    start_text: Any = snippet.get("scheduledStartTime")
    start_utc: datetime | None = _parse_start(start_text)
    if start_utc is None:
        LOGGER.warning(
            "broadcast_without_start channel=%s broadcast_id=%s value=%r",
            channel_key,
            broadcast_id,
            start_text,
        )
        return None
    category_id: Any = snippet.get("categoryId")
    return UpcomingBroadcast(
        broadcast_id=broadcast_id,
        start_utc=start_utc,
        title=_text(snippet, "title", allow_empty=True),
        description=_text(snippet, "description", allow_empty=True),
        stream_id=_bound_stream_id(item),
        category_id=category_id if isinstance(category_id, str) and category_id else None,
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


def _warn_on_unexpected_key(channel_key: str, stream: StreamInfo) -> None:
    """Ключ неожиданного вида не отбрасывается — только предупреждение (маска обязательна)."""
    if stream.stream_name and not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream.stream_name):
        LOGGER.warning(
            "stream_key_unexpected_format channel=%s stream_id=%s stream_key=%s",
            channel_key,
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
