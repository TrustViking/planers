"""YouTube Data API v3 (ТЗ §7.3, §7.4): чтение каналов, эфиров и потоков.

Задача 3a — только чтение: describe_channel, list_upcoming, get_stream. Создание и
исправление эфиров появятся в задаче 3b. Любой сбой наружу — только PlatformError.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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

# Формат ключа потока YouTube (ТЗ §7.4) — единственный источник.
YOUTUBE_STREAM_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]{4}(-[a-z0-9]{4}){3,4}$")

# Коды ошибок площадки, которые планер называет сам (ответа Google за ними нет).
ERROR_CHANNEL_NOT_FOUND: Final[str] = "channelNotFound"
ERROR_AUTH: Final[str] = "authFailed"
ERROR_TRANSPORT: Final[str] = "transportFailed"
ERROR_BAD_RESPONSE: Final[str] = "badResponse"
ERROR_NOT_IMPLEMENTED: Final[str] = "notImplementedYet"
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

    def create_broadcast(
        self,
        channel: ChannelConfig,
        spec: BroadcastSpec,
        preview: bytes | None,
    ) -> CreatedBroadcast:
        raise PlatformError(ERROR_NOT_IMPLEMENTED, "create_broadcast appears in task 3c")

    def update_broadcast(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
        preview: bytes | None,
    ) -> None:
        raise PlatformError(ERROR_NOT_IMPLEMENTED, "update_broadcast appears in task 3c")

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
    return UpcomingBroadcast(
        broadcast_id=broadcast_id,
        start_utc=start_utc,
        title=_text(snippet, "title", allow_empty=True),
        description=_text(snippet, "description", allow_empty=True),
        stream_id=_bound_stream_id(item),
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
