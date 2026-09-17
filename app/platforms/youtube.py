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
import random
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from app.config.loader import ChannelConfig
from app.google.auth import AuthError, AuthErrorReason, load_credentials, save_token, token_file_for
from app.core.dates import format_datetime_text
from app.core.retry import RetryPolicy
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
    LOGIN_REQUIRED_CODE,
    PLACEHOLDER_TOKEN,
    broadcast_url_for,
    picture_sha,
    placeholder_sha_from_description,
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
# Картинка эфира для сверки обложки: самый маленький размер; скачивание квоту не тратит.
THUMBNAIL_PICTURE_SIZE: Final[str] = "default"
PICTURE_TIMEOUT_SEC: Final[int] = 15
HTTP_OK: Final[int] = 200

# Формат ключа потока YouTube (ТЗ §7.4) — единственный источник.
YOUTUBE_STREAM_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]{4}(-[a-z0-9]{4}){3,4}$")

# Коды ошибок площадки, которые планер называет сам (ответа Google за ними нет).
ERROR_CHANNEL_NOT_FOUND: Final[str] = "channelNotFound"
ERROR_AUTH: Final[str] = "authFailed"
ERROR_LOGIN_REQUIRED: Final[str] = LOGIN_REQUIRED_CODE   # вход нужен, но запрещён (allow_login=False)
ERROR_TRANSPORT: Final[str] = "transportFailed"
ERROR_BAD_RESPONSE: Final[str] = "badResponse"
ERROR_UNEXPECTED_KEY: Final[str] = "unexpectedStreamKeyFormat"
ERROR_UNKNOWN: Final[str] = "unknown"

LOG_MISSING: Final[str] = "-"   # чего площадка не прислала: строка лога остаётся key=value

RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()
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
    ERROR_LOGIN_REQUIRED: ErrorBehavior.CALL,   # вход запретил вызывающий: обычный вызов потом войдёт
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
# Ключ памяти отказов: (ключ канала, операция); None — «любой».
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


def _refusal_key(behavior: ErrorBehavior, channel_key: str, operation: str) -> RefusalKey | None:
    if behavior is ErrorBehavior.OPERATION:
        return (channel_key, operation)
    if behavior is ErrorBehavior.CHANNEL:
        return (channel_key, None)
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
    """Клиент строится лениво и кешируется по channel.key: один токен — один канал.

    Настройки эфира площадка не хранит: всё, что уходит в эфир, приходит готовой спекой (§7.4).
    request_pause_sec — youtube_pause_seconds из planer.json: наименьший промежуток между обращениями.
    rng — случайная добавка к паузам повторов (RetryPolicy); в тестах — фиксированный.
    Браузер открывается только из describe_channel(allow_login=True): все прочие обращения входа не делают.
    Учётные данные нового входа живут в памяти, пока канал не подтверждён (keep_login пишет токен).
    """

    def __init__(
        self,
        client_secret_file: Path,
        secrets_dir: Path,
        *,
        request_pause_sec: int,
        rng: random.Random,
    ) -> None:
        self._client_secret_file: Path = client_secret_file
        self._secrets_dir: Path = secrets_dir
        self._request_pause_sec: int = request_pause_sec
        self._rng: random.Random = rng
        self._last_request_at: float | None = None   # time.monotonic() конца предыдущего обращения
        self._refusals: dict[RefusalKey, tuple[PlatformError, ErrorBehavior]] = {}
        self._session: requests.Session = requests.Session()   # картинки эфиров (i.ytimg.com), без авторизации
        self._services: dict[str, Any] = {}
        self._channels: dict[str, ChannelInfo] = {}
        self._new_logins: dict[str, Any] = {}   # channel.key → учётные данные входа, ещё не записанные в файл
        self._fresh_logins: set[str] = set()    # channel.key, у которых следующий вход — браузером, без токена
        self._notices: list[PlatformNotice] = []   # замечания за запуск; забирает take_notices

    @property
    def limits(self) -> PlatformLimits:
        return PlatformLimits(
            title_max_chars=YOUTUBE_TITLE_MAX_CHARS,
            description_max_chars=YOUTUBE_DESCRIPTION_MAX_CHARS,
            auto_stop=ENABLE_AUTO_STOP,
            latency_preference=LATENCY_PREFERENCE,
        )

    def describe_channel(self, channel: ChannelConfig, *, allow_login: bool = True) -> ChannelInfo:
        """Кеш на процесс: за запуск канал спрашивается один раз (квота §6.1 п.3).

        allow_login=False — без браузера: токена нет или он отозван — PlatformError(ERROR_LOGIN_REQUIRED).
        """
        cached: ChannelInfo | None = self._channels.get(channel.key)
        if cached is not None:
            return cached
        response: dict[str, Any] = self._execute(
            channel,
            "channels.list",
            lambda service: service.channels().list(part=CHANNEL_PARTS, mine=True),
            allow_login=allow_login,
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
            handle_raw=_optional_text(snippet, "customUrl"),
        )
        LOGGER.info(
            'channel_described channel="%s" handle=%s handle_raw=%s youtube_channel_id=%s youtube_title="%s" language=%s',
            channel.account_name,
            channel.handle,
            info.handle_raw or LOG_MISSING,
            info.youtube_channel_id,
            info.title,
            info.default_language or LOG_MISSING,
        )
        self._channels[channel.key] = info
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
            items: list[dict[str, Any]] = _items(response)
            page_broadcasts, page_notices = _broadcasts_from_page(items, channel)
            broadcasts.extend(self._with_picture(broadcast, items) for broadcast in page_broadcasts)
            self._notices.extend(page_notices)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        LOGGER.info('broadcasts_listed channel="%s" handle=%s count=%d', channel.account_name, channel.handle, len(broadcasts))
        return broadcasts

    def _with_picture(self, broadcast: UpcomingBroadcast, items: list[dict[str, Any]]) -> UpcomingBroadcast:
        """Отпечаток картинки эфира — по snippet.thumbnails того же ответа списка."""
        for item in items:
            if item.get("id") == broadcast.broadcast_id:
                thumbnails: dict[str, Any] = _mapping(_mapping(item, "snippet"), "thumbnails")
                return replace(broadcast, thumbnail_sha=self._picture_sha(thumbnails))
        return broadcast

    def _picture_sha(self, thumbnails: dict[str, Any]) -> str | None:
        """Картинка размера default → отпечаток. Не скачалась — None: это не отказ площадки и не сбой канала."""
        size: Any = thumbnails.get(THUMBNAIL_PICTURE_SIZE)
        url: Any = size.get("url") if isinstance(size, dict) else None
        if not isinstance(url, str) or not url:
            return None
        self._wait_pause()
        try:
            response: requests.Response = self._session.get(url, timeout=PICTURE_TIMEOUT_SEC)
        except requests.RequestException as error:
            LOGGER.info("thumbnail_picture_unavailable url=%s status=%s error=%s", url, LOG_MISSING, error)
            return None
        finally:
            self._mark_request_done()
        if response.status_code != HTTP_OK or not response.content:
            LOGGER.info("thumbnail_picture_unavailable url=%s status=%s", url, response.status_code)
            return None
        return picture_sha(response.content)

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
            description=_text(_mapping(item, "snippet"), "description", allow_empty=True),
        )
        _warn_on_unexpected_key(channel, stream)
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
        LOGGER.info('broadcast_inserted channel="%s" handle=%s broadcast_id=%s', channel.account_name, channel.handle, broadcast_id)
        # обложки у нового эфира ещё нет: его картинка — заглушка канала, её отпечаток уходит в описание потока
        placeholder: str | None = self._picture_sha(_mapping(_mapping(response, "snippet"), "thumbnails"))
        LOGGER.info(
            'thumbnail_placeholder_captured channel="%s" handle=%s broadcast_id=%s sha=%s',
            channel.account_name,
            channel.handle,
            broadcast_id,
            placeholder or LOG_MISSING,
        )
        return self._attach_new_stream(channel, broadcast_id, spec, placeholder)

    def attach_stream(
        self,
        channel: ChannelConfig,
        broadcast_id: str,
        spec: BroadcastSpec,
    ) -> CreatedBroadcast:
        """Эфир уже есть, потока нет: тот же путь, начиная с liveStreams.insert.

        Отпечаток заглушки не снимается: на картинке существующего эфира может быть обложка.
        """
        return self._attach_new_stream(channel, broadcast_id, spec, None)

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
        LOGGER.info('broadcast_updated channel="%s" handle=%s broadcast_id=%s', channel.account_name, channel.handle, broadcast_id)

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
            'video_settings_applied channel="%s" handle=%s broadcast_id=%s language=%s category=%s audience=%s privacy=%s'
            ' applied_language=%s applied_category=%s applied_privacy=%s',
            channel.account_name,
            channel.handle,
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
        # отпечаток заглушки из прежнего описания сохраняется: по нему следующий запуск узнает эфир без обложки
        previous: str = _text(snippet, "description", allow_empty=True)
        snippet["title"] = marker
        snippet["description"] = stream_description(
            channel, marker, _local_now(), placeholder_sha_from_description(previous)
        )
        self._execute(
            channel,
            "liveStreams.update",
            lambda service: service.liveStreams().update(
                part=STREAM_UPDATE_PARTS,
                body={"id": stream_id, "snippet": snippet},
            ),
        )
        LOGGER.info('stream_marker_set channel="%s" handle=%s stream_id=%s marker=%s', channel.account_name, channel.handle, stream_id, marker)

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
        parsed: UpcomingBroadcast | PlatformNotice | None = _broadcast_from_item(items[0], channel)
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
        placeholder_sha: str | None,
    ) -> CreatedBroadcast:
        """liveStreams.insert → проверка ключа → bind. Один поток на эфир (§7.4)."""
        response: dict[str, Any] = self._execute(
            channel,
            "liveStreams.insert",
            lambda service: service.liveStreams().insert(
                part=STREAM_PARTS,
                body=_stream_body(channel, spec, placeholder_sha),
            ),
        )
        stream_id: str = _text(response, "id")
        ingestion: dict[str, Any] = _mapping(_mapping(response, "cdn"), "ingestionInfo")
        stream_key: str = _text(ingestion, "streamName")
        if not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream_key):
            LOGGER.error(
                'stream_key_rejected channel="%s" handle=%s stream_id=%s stream_key=%s',
                channel.account_name,
                channel.handle,
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
            'stream_bound channel="%s" handle=%s broadcast_id=%s stream_id=%s stream_key=%s',
            channel.account_name,
            channel.handle,
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
        LOGGER.info('thumbnail_set channel="%s" handle=%s broadcast_id=%s', channel.account_name, channel.handle, broadcast_id)

    def keep_login(self, channel: ChannelConfig) -> None:
        """Канал подтверждён: учётные данные нового входа — в файл токена; нового входа не было — ничего."""
        credentials: Any = self._new_logins.pop(channel.key, None)
        if credentials is None:
            return
        self._fresh_logins.discard(channel.key)
        token_file: Path = token_file_for(self._secrets_dir, channel.handle)
        try:
            save_token(credentials, token_file)
        except AuthError as error:
            raise PlatformError(ERROR_AUTH, f"{error.reason.value}: {error.detail}") from error
        LOGGER.info('token_created channel="%s" handle=%s file=%s', channel.account_name, channel.handle, token_file.name)

    def drop_login(self, channel: ChannelConfig) -> None:
        """Клиент, учётные данные и ChannelInfo канала забыты; следующий вход — браузером, токен не читается."""
        self._services.pop(channel.key, None)
        self._channels.pop(channel.key, None)
        self._new_logins.pop(channel.key, None)
        self._fresh_logins.add(channel.key)
        for key in [key for key in self._refusals if key[0] == channel.key]:
            del self._refusals[key]
        LOGGER.info('login_dropped channel="%s" handle=%s', channel.account_name, channel.handle)

    def _service(self, channel: ChannelConfig, allow_login: bool) -> Any:
        cached: Any = self._services.get(channel.key)
        if cached is not None:
            return cached
        is_fresh: bool = channel.key in self._fresh_logins
        logged_in: list[bool] = []
        try:
            credentials: Any = load_credentials(
                self._client_secret_file,
                token_file_for(self._secrets_dir, channel.handle),
                login_hint=channel.google_account,
                force_reauth=is_fresh,
                on_login=lambda: logged_in.append(True),
                allow_login=allow_login,
            )
        except AuthError as error:
            code: str = ERROR_LOGIN_REQUIRED if error.reason is AuthErrorReason.LOGIN_REQUIRED else ERROR_AUTH
            raise PlatformError(code, f"{error.reason.value}: {error.detail}") from error
        if logged_in:
            # токен пишет keep_login — только когда канал за этим входом подтверждён
            self._new_logins[channel.key] = credentials
        service: Any = build(API_SERVICE_NAME, API_VERSION, credentials=credentials, cache_discovery=False)
        self._services[channel.key] = service
        return service

    def _execute(
        self,
        channel: ChannelConfig,
        operation: str,
        request_builder: Any,
        *,
        allow_login: bool = False,
    ) -> dict[str, Any]:
        """Единственная точка обращения к API: память отказов, пауза, повторы по _error_behavior и RETRY_POLICY.

        Вход в браузере — только когда его явно разрешил describe_channel(allow_login=True).
        """
        self._raise_if_refused(channel, operation)
        service: Any = self._channel_service(channel, operation, allow_login)
        retry_number: int = 0
        while True:
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
            if failure.behavior is ErrorBehavior.RETRY and RETRY_POLICY.has_retry_left(retry_number + 1):
                retry_number += 1
                self._sleep_before_retry(channel, operation, retry_number, failure)
                continue
            raise self._refuse(channel, operation, failure)

    def _wait_pause(self) -> None:
        """Выждать остаток request_pause_sec от конца предыдущего обращения; паузы повторов входят в него."""
        if self._request_pause_sec <= 0 or self._last_request_at is None:
            return
        remaining: float = self._request_pause_sec - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _mark_request_done(self) -> None:
        self._last_request_at = time.monotonic()

    def _channel_service(self, channel: ChannelConfig, operation: str, allow_login: bool) -> Any:
        """Клиент канала; отказ входа — такой же отказ, как у запроса (ERROR_AUTH → CHANNEL).

        Запрещённый вход — не отказ YouTube: не запоминается и в лог отказов не пишется.
        """
        try:
            return self._service(channel, allow_login)
        except PlatformError as error:
            if error.code == ERROR_LOGIN_REQUIRED:
                raise
            behavior: ErrorBehavior = _error_behavior(operation, None, error.code)
            raise self._refuse(channel, operation, _Failure(error, behavior, None)) from error

    def _raise_if_refused(self, channel: ChannelConfig, operation: str) -> None:
        """Запомненный отказ поднимается без обращения к сети и без паузы."""
        for key in ((None, None), (channel.key, None), (channel.key, operation)):
            remembered: tuple[PlatformError, ErrorBehavior] | None = self._refusals.get(key)
            if remembered is None:
                continue
            error, behavior = remembered
            LOGGER.info(
                'request_skipped operation=%s channel="%s" handle=%s reason=%s behavior=%s',
                operation,
                channel.account_name,
                channel.handle,
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
            'youtube_refused operation=%s channel="%s" handle=%s http_status=%s reason=%s behavior=%s message="%s"',
            operation,
            channel.account_name,
            channel.handle,
            failure.http_status if failure.http_status is not None else LOG_MISSING,
            error.code,
            failure.behavior.value,
            error.message,
        )
        key: RefusalKey | None = _refusal_key(failure.behavior, channel.key, operation)
        if key is not None:
            self._refusals[key] = (error, failure.behavior)
        return error

    def _sleep_before_retry(self, channel: ChannelConfig, operation: str, retry_number: int, failure: _Failure) -> None:
        delay_sec: float = RETRY_POLICY.delay_sec(retry_number, self._rng)
        LOGGER.warning(
            'request_retry operation=%s channel="%s" handle=%s retry=%d/%d delay_sec=%.2f http_status=%s reason=%s',
            operation,
            channel.account_name,
            channel.handle,
            retry_number,
            RETRY_POLICY.max_retries,
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


def stream_description(
    channel: ChannelConfig,
    marker: str,
    written_at: datetime,
    placeholder_sha: str | None = None,
) -> str:
    """Описание ключа потока в Студии: чей это ключ и для какого эфира; отпечаток заглушки — если известен."""
    parts: MarkerParts | None = split_marker(marker)
    text: str = msg.STREAM_DESCRIPTION.format(
        account_name=channel.account_name,
        handle=channel.handle,
        date=parts.date if parts else marker,
        time=parts.time if parts else "",
        language=parts.language if parts else "",
        written_at=format_datetime_text(written_at),
    )
    if placeholder_sha is None:
        return text
    return text + msg.STREAM_DESCRIPTION_PLACEHOLDER.format(token=PLACEHOLDER_TOKEN.format(sha=placeholder_sha))


def _stream_body(channel: ChannelConfig, spec: BroadcastSpec, placeholder_sha: str | None) -> dict[str, Any]:
    """Название потока — маркер планера (§7.3), описание — чей это ключ; зрителям не видны."""
    return {
        "snippet": {
            "title": spec.marker,
            "description": stream_description(channel, spec.marker, _local_now(), placeholder_sha),
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
    channel: ChannelConfig,
) -> tuple[list[UpcomingBroadcast], list[PlatformNotice]]:
    """Эфиры страницы и замечания о тех, что сверять не с чем."""
    broadcasts: list[UpcomingBroadcast] = []
    notices: list[PlatformNotice] = []
    for item in items:
        parsed: UpcomingBroadcast | PlatformNotice | None = _broadcast_from_item(item, channel)
        if isinstance(parsed, UpcomingBroadcast):
            broadcasts.append(parsed)
        elif parsed is not None:
            notices.append(parsed)
    return broadcasts, notices


def _broadcast_from_item(item: dict[str, Any], channel: ChannelConfig) -> UpcomingBroadcast | PlatformNotice | None:
    """Эфир без разбираемого времени старта сверять не с чем: вместо эфира — замечание для владельца.

    Постоянный эфир канала (isDefaultBroadcast) владелец не удалит и не исправит: без замечания, None.
    """
    snippet: dict[str, Any] = _mapping(item, "snippet")
    broadcast_id: str = _text(item, "id")
    start_text: Any = snippet.get("scheduledStartTime")
    start_utc: datetime | None = _parse_start(start_text)
    if start_utc is None and _optional_bool(snippet, DEFAULT_BROADCAST_FLAG):
        LOGGER.info(
            'default_broadcast_skipped channel="%s" handle=%s broadcast_id=%s title=%r',
            channel.account_name,
            channel.handle,
            broadcast_id,
            snippet.get("title"),
        )
        return None
    if start_utc is None:
        # лог — только диагностика; владельцу факт уходит данными (PlatformNotice → take_notices)
        LOGGER.info(
            'broadcast_without_start channel="%s" handle=%s broadcast_id=%s title=%r value=%r',
            channel.account_name,
            channel.handle,
            broadcast_id,
            snippet.get("title"),
            start_text,
        )
        return PlatformNotice(
            kind=PlatformNoticeKind.UNDATED_BROADCAST,
            account_name=channel.account_name,
            title=str(snippet.get("title") or ""),
            handle=channel.handle,
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


def _warn_on_unexpected_key(channel: ChannelConfig, stream: StreamInfo) -> None:
    """Ключ неожиданного вида не отбрасывается — только предупреждение (маска обязательна)."""
    if stream.stream_name and not YOUTUBE_STREAM_KEY_PATTERN.fullmatch(stream.stream_name):
        LOGGER.warning(
            'stream_key_unexpected_format channel="%s" handle=%s stream_id=%s stream_key=%s',
            channel.account_name,
            channel.handle,
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
