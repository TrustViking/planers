from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests
from googleapiclient.errors import HttpError

from dataclasses import replace

from app.config.loader import ChannelConfig, Platform, Privacy
from app.platforms import youtube as youtube_module
from app.pipeline.plan import BroadcastSpec
from app.platforms.base import (
    BroadcastFacts,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    PlatformNotice,
    PlatformNoticeKind,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
    picture_sha,
    placeholder_sha_from_description,
)
from app.google.auth import AuthError, AuthErrorReason
from app.platforms.youtube import (
    ERROR_AUTH,
    ERROR_LOGIN_REQUIRED,
    RETRY_POLICY,
    YOUTUBE_STREAM_KEY_PATTERN,
    ErrorBehavior,
    YouTubePlatform,
    _error_behavior,
)

CHANNEL: ChannelConfig = ChannelConfig(
    platform=Platform.YOUTUBE,
    account_name="Канал UA",
    handle="@KanalUA",
    google_account="owner@gmail.com",
    languages=("uk",),
    privacy=Privacy.PUBLIC,
)
OTHER_CHANNEL: ChannelConfig = replace(
    CHANNEL, account_name="Канал RU", handle="@KanalRU", google_account="ru@gmail.com"
)
GOOD_KEY: str = "abcd-1234-efgh-5678-ijkl"
RNG_SEED: int = 7


def _expected_delays(count: int) -> list[float]:
    """Паузы повторов 1..count по RetryPolicy с тем же фиксированным rng, что у площадки в тестах."""
    rng: random.Random = random.Random(RNG_SEED)
    return [RETRY_POLICY.delay_sec(number, rng) for number in range(1, count + 1)]


class _FakeRequest:
    def __init__(self, response: Any) -> None:
        self._response: Any = response

    def execute(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClock:
    """time для youtube.py: sleep двигает часы, сами запросы времени не занимают."""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeResource:
    """Ресурс googleapiclient: .list(**kwargs) → объект с .execute()."""

    def __init__(
        self,
        responses: list[Any],
        calls: list[dict[str, Any]],
        name: str,
        clock: _FakeClock | None = None,
    ) -> None:
        self._responses: list[Any] = responses
        self._calls: list[dict[str, Any]] = calls
        self._name: str = name
        self._clock: _FakeClock | None = clock

    def list(self, **kwargs: Any) -> _FakeRequest:
        return self._call("list", **kwargs)

    def __getattr__(self, method: str) -> Any:
        """insert, bind, update, set — все ведут себя одинаково: очередь ответов."""
        if method.startswith("_"):
            raise AttributeError(method)
        return lambda **kwargs: self._call(method, **kwargs)

    def _call(self, method: str, **kwargs: Any) -> _FakeRequest:
        call: dict[str, Any] = {"resource": self._name, "method": method, **kwargs}
        if self._clock is not None:
            call["at"] = self._clock.now      # момент запроса: для проверки паузы
        self._calls.append(call)
        if not self._responses:
            raise AssertionError(f"неожиданный вызов {self._name}.{method}")
        return _FakeRequest(self._responses.pop(0))


class _FakeService:
    def __init__(self, clock: _FakeClock | None = None, **responses: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._clock: _FakeClock | None = clock
        self._resources: dict[str, _FakeResource] = {
            name: _FakeResource(list(items), self.calls, name, clock) for name, items in responses.items()
        }

    def _resource(self, name: str) -> _FakeResource:
        return self._resources.setdefault(name, _FakeResource([], self.calls, name, self._clock))

    def channels(self) -> _FakeResource:
        return self._resource("channels")

    def liveBroadcasts(self) -> _FakeResource:  # noqa: N802 — имя как у googleapiclient
        return self._resource("liveBroadcasts")

    def liveStreams(self) -> _FakeResource:  # noqa: N802 — имя как у googleapiclient
        return self._resource("liveStreams")

    def videos(self) -> _FakeResource:
        return self._resource("videos")

    def thumbnails(self) -> _FakeResource:
        return self._resource("thumbnails")


@pytest.fixture
def platform(tmp_path: Path) -> YouTubePlatform:
    client_secret: Path = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    return YouTubePlatform(client_secret, tmp_path, request_pause_sec=0, rng=random.Random(RNG_SEED))


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Часы и паузы youtube.py — поддельные: повторы и пауза между обращениями проверяются без ожидания."""
    fake: _FakeClock = _FakeClock()
    monkeypatch.setattr(youtube_module, "time", fake)
    return fake


def _install(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    service: _FakeService,
) -> _FakeService:
    """Готовый клиент вместо build() и OAuth: сеть в тестах запрещена."""
    monkeypatch.setattr(platform, "_service", lambda channel, allow_login: service)
    return service


def _http_error(status: int, reason: str, message: str) -> HttpError:
    content: bytes = json.dumps(
        {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    ).encode("utf-8")
    return HttpError(resp=type("Resp", (), {"status": status, "reason": message})(), content=content)


def _broadcast_item(broadcast_id: str, start: str, stream_id: str | None = "S1") -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": broadcast_id,
        "snippet": {"title": f"Эфир {broadcast_id}", "description": "Описание", "scheduledStartTime": start},
        "contentDetails": {},
    }
    if stream_id is not None:
        item["contentDetails"]["boundStreamId"] = stream_id
    return item


def test_describe_channel_reads_id_title_and_language(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(
            channels=[
                {
                    "items": [
                        {
                            "id": "UC123",
                            "snippet": {"title": "Мой канал", "defaultLanguage": "ru"},
                            "brandingSettings": {"channel": {"defaultLanguage": "uk"}},
                        }
                    ]
                }
            ]
        ),
    )
    info: ChannelInfo = platform.describe_channel(CHANNEL)
    assert info == ChannelInfo(youtube_channel_id="UC123", title="Мой канал", default_language="uk")
    assert service.calls[0]["mine"] is True


def test_describe_channel_falls_back_to_snippet_language(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        platform,
        monkeypatch,
        _FakeService(channels=[{"items": [{"id": "UC1", "snippet": {"title": "X", "defaultLanguage": "ru"}}]}]),
    )
    assert platform.describe_channel(CHANNEL).default_language == "ru"


def test_describe_channel_without_language_gives_none(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(platform, monkeypatch, _FakeService(channels=[{"items": [{"id": "UC1", "snippet": {"title": "X"}}]}]))
    assert platform.describe_channel(CHANNEL).default_language is None


def test_describe_channel_without_items_is_an_error(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(platform, monkeypatch, _FakeService(channels=[{"items": []}]))
    with pytest.raises(PlatformError) as raised:
        platform.describe_channel(CHANNEL)
    assert raised.value.code == "channelNotFound"


def test_list_upcoming_walks_all_pages(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(
            liveBroadcasts=[
                {"items": [_broadcast_item("B1", "2027-03-17T17:00:00Z")], "nextPageToken": "page2"},
                {"items": [_broadcast_item("B2", "2027-03-18T17:00:00Z")]},
            ]
        ),
    )
    broadcasts: list[UpcomingBroadcast] = platform.list_upcoming(CHANNEL)
    assert [item.broadcast_id for item in broadcasts] == ["B1", "B2"]
    assert broadcasts[0].start_utc == datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)
    assert [call.get("pageToken") for call in service.calls] == [None, "page2"]
    # фильтры id / mine / broadcastStatus взаимоисключающие: mine отправлять нельзя (400)
    assert all("mine" not in call for call in service.calls)
    assert all(call["broadcastStatus"] == "upcoming" for call in service.calls)


def test_list_upcoming_accepts_empty_channel(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": []}]))
    assert platform.list_upcoming(CHANNEL) == []


def test_broadcast_without_bound_stream_has_none(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[{"items": [_broadcast_item("B1", "2027-03-17T17:00:00Z", stream_id=None)]}]),
    )
    [broadcast] = platform.list_upcoming(CHANNEL)
    assert broadcast.stream_id is None
    assert broadcast.description == "Описание"


def test_broadcast_with_offset_start_is_converted_to_utc(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[{"items": [_broadcast_item("B1", "2027-03-17T19:00:00+02:00")]}]),
    )
    [broadcast] = platform.list_upcoming(CHANNEL)
    assert broadcast.start_utc == datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)


def test_broadcast_without_start_is_skipped(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    item["snippet"].pop("scheduledStartTime")
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [item]}]))
    with caplog.at_level("DEBUG"):
        assert platform.list_upcoming(CHANNEL) == []
    # в лог — INFO, только диагностика: данных для владельца в записи лога больше нет
    [record] = [record for record in caplog.records if "broadcast_without_start" in record.getMessage()]
    assert record.levelname == "INFO" and "Эфир B1" in record.getMessage()


def test_undated_broadcast_becomes_one_notice_taken_once(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Боевой разбор liveBroadcasts.list: эфир без scheduledStartTime выпадает из списка и даёт одно замечание."""
    undated: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    undated["snippet"].pop("scheduledStartTime")
    dated: dict[str, Any] = _broadcast_item("B2", "2027-03-17T17:00:00Z")
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [undated, dated]}]))
    assert [broadcast.broadcast_id for broadcast in platform.list_upcoming(CHANNEL)] == ["B2"]
    assert platform.take_notices() == (
        PlatformNotice(PlatformNoticeKind.UNDATED_BROADCAST, account_name="Канал UA", title="Эфир B1", handle="@KanalUA"),
    )
    assert platform.take_notices() == ()                 # накопитель очищен


def test_default_broadcast_without_start_gives_neither_broadcast_nor_notice(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Постоянный эфир канала заводит сама площадка: владельцу с ним делать нечего, остаётся строка лога."""
    permanent: dict[str, Any] = _broadcast_item("B0", "2027-03-17T17:00:00Z")
    permanent["snippet"].pop("scheduledStartTime")
    permanent["snippet"]["isDefaultBroadcast"] = True
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [permanent]}]))
    with caplog.at_level("DEBUG"):
        assert platform.list_upcoming(CHANNEL) == []
    assert platform.take_notices() == ()
    messages: list[str] = [record.getMessage() for record in caplog.records]
    [record] = [record for record in caplog.records if "default_broadcast_skipped" in record.getMessage()]
    assert record.levelname == "INFO" and "broadcast_id=B0" in record.getMessage() and "Эфир B0" in record.getMessage()
    assert not any("broadcast_without_start" in message for message in messages)


def test_ordinary_undated_broadcast_next_to_default_still_gives_one_notice(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permanent: dict[str, Any] = _broadcast_item("B0", "2027-03-17T17:00:00Z")
    permanent["snippet"].pop("scheduledStartTime")
    permanent["snippet"]["isDefaultBroadcast"] = True
    undated: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    undated["snippet"].pop("scheduledStartTime")
    undated["snippet"]["isDefaultBroadcast"] = False
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [permanent, undated]}]))
    assert platform.list_upcoming(CHANNEL) == []
    assert platform.take_notices() == (
        PlatformNotice(PlatformNoticeKind.UNDATED_BROADCAST, account_name="Канал UA", title="Эфир B1", handle="@KanalUA"),
    )


def test_list_upcoming_reads_privacy_and_content_details(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Части status и contentDetails уже запрашиваются: новых вызовов API нет."""
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    item["status"] = {"privacyStatus": "private"}
    item["contentDetails"].update({"enableAutoStart": False, "enableAutoStop": True, "latencyPreference": "low"})
    service: _FakeService = _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [item]}]))
    [broadcast] = platform.list_upcoming(CHANNEL)
    assert (broadcast.privacy_status, broadcast.auto_start, broadcast.auto_stop, broadcast.latency_preference) == (
        "private",
        False,
        True,
        "low",
    )
    assert len(service.calls) == 1 and service.calls[0]["part"] == "snippet,contentDetails,status"


def test_get_stream_reads_marker_and_key(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        platform,
        monkeypatch,
        _FakeService(
            liveStreams=[
                {
                    "items": [
                        {
                            "id": "S1",
                            "snippet": {"title": "17-03-2027_1900_uk"},
                            "cdn": {
                                "ingestionInfo": {
                                    "ingestionAddress": "rtmp://a.rtmp.youtube.com/live2",
                                    "streamName": GOOD_KEY,
                                }
                            },
                        }
                    ]
                }
            ]
        ),
    )
    stream: StreamInfo | None = platform.get_stream(CHANNEL, "S1")
    assert stream == StreamInfo(
        stream_id="S1",
        title="17-03-2027_1900_uk",
        ingestion_address="rtmp://a.rtmp.youtube.com/live2",
        stream_name=GOOD_KEY,
    )


def test_get_stream_without_items_gives_none(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(platform, monkeypatch, _FakeService(liveStreams=[{"items": []}]))
    assert platform.get_stream(CHANNEL, "S1") is None


def test_unexpected_key_format_warns_but_keeps_stream(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install(
        platform,
        monkeypatch,
        _FakeService(
            liveStreams=[
                {
                    "items": [
                        {
                            "id": "S1",
                            "snippet": {"title": "маркер"},
                            "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://x", "streamName": "STRANGE-KEY"}},
                        }
                    ]
                }
            ]
        ),
    )
    with caplog.at_level("WARNING"):
        stream: StreamInfo | None = platform.get_stream(CHANNEL, "S1")
    assert stream is not None and stream.stream_name == "STRANGE-KEY"
    assert "stream_key_unexpected_format" in caplog.text
    assert "STRANGE-KEY" not in caplog.text          # ключ в логах только замаскированным
    assert "****-Y-KE" in caplog.text or "****" in caplog.text


def test_stream_key_pattern_matches_youtube_shapes() -> None:
    assert YOUTUBE_STREAM_KEY_PATTERN.fullmatch(GOOD_KEY)
    assert YOUTUBE_STREAM_KEY_PATTERN.fullmatch("abcd-1234-efgh-5678")
    assert not YOUTUBE_STREAM_KEY_PATTERN.fullmatch("ABCD-1234-efgh-5678")
    assert not YOUTUBE_STREAM_KEY_PATTERN.fullmatch("abcd1234efgh5678")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "liveStreamingNotEnabled"),
        (403, "insufficientPermissions"),
        (403, "quotaExceeded"),
        (404, "notFound"),
    ],
)
def test_http_error_becomes_platform_error_with_google_reason(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    reason: str,
) -> None:
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[_http_error(status, reason, "нельзя")]))
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == reason
    assert str(status) in raised.value.message


def test_transport_failure_becomes_platform_error(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[ConnectionError("нет сети")] * RETRY_POLICY.max_attempts),
    )
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == "transportFailed"
    assert len(service.calls) == RETRY_POLICY.max_attempts == 5
    assert clock.sleeps == _expected_delays(4)


def _platform_with_pause(tmp_path: Path, pause: int) -> YouTubePlatform:
    client_secret: Path = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    return YouTubePlatform(client_secret, tmp_path, request_pause_sec=pause, rng=random.Random(RNG_SEED))


def _stream_list(stream_id: str = "S1") -> dict[str, Any]:
    return {"items": [{"id": stream_id, "snippet": {"title": "m"}, "cdn": {"ingestionInfo": {}}}]}


def test_pause_keeps_requests_apart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    platform: YouTubePlatform = _platform_with_pause(tmp_path, 2)
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(clock, liveStreams=[_stream_list(), _stream_list(), _stream_list()])
    )
    for _ in range(3):
        platform.get_stream(CHANNEL, "S1")
    moments: list[float] = [call["at"] for call in service.calls]
    assert all(later - earlier >= 2 for earlier, later in zip(moments, moments[1:]))
    assert clock.sleeps == [2, 2]                       # перед первым обращением ждать нечего


def test_retry_pause_counts_toward_the_request_pause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    """Паузы повторов (от 2 с) уже покрывают паузу между обращениями — лишнего sleep нет."""
    platform: YouTubePlatform = _platform_with_pause(tmp_path, 2)
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(clock, liveBroadcasts=[_http_error(503, "backendError", "x")] * 3 + [{"items": []}]),
    )
    assert platform.list_upcoming(CHANNEL) == []
    assert clock.sleeps == _expected_delays(3)
    moments: list[float] = [call["at"] for call in service.calls]
    assert all(later - earlier >= 2 for earlier, later in zip(moments, moments[1:]))


def test_zero_pause_never_sleeps(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> None:
    _install(platform, monkeypatch, _FakeService(clock, liveStreams=[_stream_list(), _stream_list()]))
    platform.get_stream(CHANNEL, "S1")
    platform.get_stream(CHANNEL, "S1")
    assert clock.sleeps == []


@pytest.mark.parametrize(
    ("operation", "status", "reason", "behavior"),
    [
        ("thumbnails.set", 429, "uploadRateLimitExceeded", ErrorBehavior.OPERATION),
        ("thumbnails.set", 403, "forbidden", ErrorBehavior.OPERATION),
        ("liveBroadcasts.list", 403, "forbidden", ErrorBehavior.CALL),
        ("liveBroadcasts.list", 403, "quotaExceeded", ErrorBehavior.PROJECT),
        ("liveBroadcasts.list", 401, "authError", ErrorBehavior.CHANNEL),
        ("channels.list", None, ERROR_AUTH, ErrorBehavior.CHANNEL),
        ("liveBroadcasts.insert", 403, "liveStreamingNotEnabled", ErrorBehavior.OPERATION),
        ("liveBroadcasts.list", 500, "backendError", ErrorBehavior.RETRY),
        ("liveBroadcasts.list", 429, "somethingNew", ErrorBehavior.RETRY),
        ("liveBroadcasts.list", 502, "somethingNew", ErrorBehavior.RETRY),
        ("liveBroadcasts.list", 403, "somethingNew", ErrorBehavior.CALL),
        ("videos.list", 404, "videoNotFound", ErrorBehavior.CALL),
        ("channels.list", None, "channelNotFound", ErrorBehavior.CALL),
    ],
)
def test_error_behavior_table(operation: str, status: int | None, reason: str, behavior: ErrorBehavior) -> None:
    assert _error_behavior(operation, status, reason) is behavior


def test_upload_limit_stops_thumbnails_of_that_channel_only(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(
            thumbnails=[_http_error(429, "uploadRateLimitExceeded", "лимит"), {}],
            liveBroadcasts=[{"items": []}],
        ),
    )
    with pytest.raises(PlatformError) as first:
        platform.set_thumbnail(CHANNEL, "B1", b"jpg")
    assert first.value.code == "uploadRateLimitExceeded"
    assert len(service.calls) == 1 and clock.sleeps == []      # ни повторов, ни пауз повтора
    with pytest.raises(PlatformError) as second:
        platform.set_thumbnail(CHANNEL, "B2", b"jpg")          # без запроса
    assert second.value.code == "uploadRateLimitExceeded"
    assert len(service.calls) == 1
    platform.set_thumbnail(OTHER_CHANNEL, "B3", b"jpg")        # другой канал — с запросом
    platform.list_upcoming(CHANNEL)                            # другая операция того же канала — с запросом
    assert [(call["resource"], call["method"]) for call in service.calls] == [
        ("thumbnails", "set"),
        ("thumbnails", "set"),
        ("liveBroadcasts", "list"),
    ]


def test_forbidden_thumbnail_is_remembered_but_forbidden_list_is_not(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    forbidden: HttpError = _http_error(403, "forbidden", "нельзя")
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(thumbnails=[forbidden], liveBroadcasts=[forbidden, {"items": []}])
    )
    for _ in range(2):
        with pytest.raises(PlatformError, match="forbidden"):
            platform.set_thumbnail(CHANNEL, "B1", b"jpg")
    with pytest.raises(PlatformError, match="forbidden"):
        platform.list_upcoming(CHANNEL)
    assert platform.list_upcoming(CHANNEL) == []               # CALL: следующий такой же вызов идёт в сеть
    assert [call["resource"] for call in service.calls] == ["thumbnails", "liveBroadcasts", "liveBroadcasts"]


def test_quota_exceeded_stops_every_channel(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(liveBroadcasts=[_http_error(403, "quotaExceeded", "квота")])
    )
    with pytest.raises(PlatformError, match="quotaExceeded"):
        platform.list_upcoming(CHANNEL)
    with pytest.raises(PlatformError, match="quotaExceeded"):
        platform.list_upcoming(OTHER_CHANNEL)
    with pytest.raises(PlatformError, match="quotaExceeded"):
        platform.get_stream(CHANNEL, "S1")
    assert len(service.calls) == 1


def test_auth_error_stops_only_that_channel(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[_http_error(401, "authError", "токен отозван"), {"items": []}]),
    )
    with pytest.raises(PlatformError, match="authError"):
        platform.list_upcoming(CHANNEL)
    with pytest.raises(PlatformError, match="authError"):
        platform.get_stream(CHANNEL, "S1")
    assert platform.list_upcoming(OTHER_CHANNEL) == []
    assert len(service.calls) == 2


def test_failed_login_is_not_repeated(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """Отказ входа — отказ канала: браузер за запуск второй раз не открывается."""
    attempts: list[str] = []

    def _service(channel: ChannelConfig, allow_login: bool) -> Any:
        attempts.append(channel.account_name)
        raise PlatformError(ERROR_AUTH, "flow_failed: отказ")

    monkeypatch.setattr(platform, "_service", _service)
    for _ in range(2):
        with pytest.raises(PlatformError, match=ERROR_AUTH):
            platform.list_upcoming(CHANNEL)
    assert attempts == [CHANNEL.account_name]


def test_live_streaming_disabled_stops_inserts_of_that_channel(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[_http_error(403, "liveStreamingNotEnabled", "выключено"), {"items": []}]),
    )
    spec: BroadcastSpec = _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc))
    for _ in range(2):
        with pytest.raises(PlatformError, match="liveStreamingNotEnabled"):
            platform.create_broadcast(CHANNEL, spec)
    assert platform.list_upcoming(CHANNEL) == []
    assert [call["method"] for call in service.calls] == ["insert", "list"]


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (500, "backendError", "transportFailed"),
        (503, "somethingNew", "transportFailed"),
        (403, "rateLimitExceeded", "rateLimitExceeded"),
        (403, "userRateLimitExceeded", "userRateLimitExceeded"),
        (429, "somethingNew", "somethingNew"),
    ],
)
def test_retried_refusal_after_all_attempts(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
    status: int,
    reason: str,
    code: str,
) -> None:
    """5xx и сеть после всех попыток — transportFailed; лимит частоты сохраняет свою причину."""
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(liveBroadcasts=[_http_error(status, reason, "x")] * RETRY_POLICY.max_attempts)
    )
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == code
    assert len(service.calls) == RETRY_POLICY.max_attempts == 5
    assert clock.sleeps == _expected_delays(4)


def test_unknown_refusal_without_retry_status_is_asked_once(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(liveBroadcasts=[_http_error(403, "somethingNew", "x")])
    )
    with pytest.raises(PlatformError, match="somethingNew"):
        platform.list_upcoming(CHANNEL)
    assert len(service.calls) == 1 and clock.sleeps == []


def _stream_response(key: str = GOOD_KEY) -> dict[str, Any]:
    return {
        "id": "S1",
        "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a.rtmp.youtube.com/live2", "streamName": key}},
    }


def _spec(now: datetime) -> BroadcastSpec:
    return BroadcastSpec(
        start_minute=now,
        marker="17-03-2027_1900_uk",
        title="Эфир",
        description="Описание",
        privacy="public",
        category_id="22",
        auto_start=True,
        auto_stop=True,
        latency_preference="normal",
    )


def test_create_broadcast_inserts_binds_and_returns_key(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(
            liveBroadcasts=[{"id": "B1"}, {"id": "B1"}],
            liveStreams=[_stream_response()],
        ),
    )
    spec: BroadcastSpec = _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc))
    created: CreatedBroadcast = platform.create_broadcast(CHANNEL, spec)

    assert created.broadcast_id == "B1"
    assert created.broadcast_url == "https://www.youtube.com/watch?v=B1"
    assert created.stream_key == GOOD_KEY
    assert created.stream_url == "rtmp://a.rtmp.youtube.com/live2"
    insert: dict[str, Any] = service.calls[0]["body"]
    assert insert["snippet"]["scheduledStartTime"] == "2027-03-17T17:00:00Z"
    assert insert["snippet"]["title"] == "Эфир"
    assert insert["snippet"]["categoryId"] == spec.category_id
    assert insert["status"]["privacyStatus"] == "public"
    assert insert["status"]["selfDeclaredMadeForKids"] is False
    assert insert["contentDetails"] == {
        "enableAutoStart": True,
        "enableAutoStop": True,
        "latencyPreference": "normal",
    }
    stream_body: dict[str, Any] = service.calls[1]["body"]
    assert stream_body["snippet"]["title"] == spec.marker      # маркер планера (§7.3)
    # описание ключа в Студии: чей ключ и для какого эфира
    assert stream_body["snippet"]["description"].startswith("Ключ планера: канал «Канал UA» @KanalUA, эфир 17-03-2027 19:00, язык uk")
    assert stream_body["cdn"] == {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"}
    assert service.calls[2]["method"] == "bind"
    assert (service.calls[2]["id"], service.calls[2]["streamId"]) == ("B1", "S1")


def test_broadcast_body_comes_from_the_spec(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """Всё, что уходит в эфир, — из спеки: там же оно сверяется с площадкой."""
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[{"id": "B1"}, {"id": "B1"}, {"id": "B1"}], liveStreams=[_stream_response()]),
    )
    spec: BroadcastSpec = replace(
        _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)),
        auto_start=False,
        category_id="25",
        privacy="unlisted",
        latency_preference="low",
    )
    platform.create_broadcast(CHANNEL, spec)
    platform.update_broadcast(CHANNEL, "B1", spec)
    insert: dict[str, Any] = service.calls[0]["body"]
    assert insert["contentDetails"]["enableAutoStart"] is False
    assert insert["contentDetails"]["latencyPreference"] == "low"
    assert insert["status"]["privacyStatus"] == "unlisted"
    assert insert["snippet"]["categoryId"] == "25"
    assert service.calls[3]["body"]["snippet"]["categoryId"] == "25"


class _Credentials:
    def to_json(self) -> str:
        return '{"token": "new"}'


def test_new_login_is_kept_in_memory_until_the_channel_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Токен ищется по нику; новый вход файл не пишет — это делает keep_login после подтверждения."""
    client_secret: Path = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    platform: YouTubePlatform = YouTubePlatform(client_secret, tmp_path, request_pause_sec=0, rng=random.Random(RNG_SEED))
    calls: list[tuple[Path, str, bool, bool]] = []

    def _load(
        client_secret_file: Path,
        token_file: Path,
        login_hint: str,
        force_reauth: bool = False,
        on_login: Any = None,
        allow_login: bool = True,
    ) -> object:
        calls.append((token_file, login_hint, force_reauth, allow_login))
        on_login()                                     # браузер открылся: вход новый
        return _Credentials()

    monkeypatch.setattr(youtube_module, "load_credentials", _load)
    monkeypatch.setattr(youtube_module, "build", lambda *args, **kwargs: _FakeService())
    token_file: Path = tmp_path / "@KanalUA.token.json"
    token_file.write_text("old", encoding="utf-8")
    platform.drop_login(CHANNEL)
    platform._service(CHANNEL, True)
    platform._service(CHANNEL, True)                   # клиент кешируется: вход один раз
    assert calls == [(token_file, "owner@gmail.com", True, True)]   # токен на диске не читается
    assert token_file.read_text(encoding="utf-8") == "old"
    platform.keep_login(CHANNEL)
    assert token_file.read_text(encoding="utf-8") == '{"token": "new"}'
    platform.keep_login(CHANNEL)                        # второй раз записывать нечего
    assert token_file.read_text(encoding="utf-8") == '{"token": "new"}'


def test_dropped_login_is_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Канал не тот: drop_login забывает учётные данные — keep_login после него ничего не пишет."""
    client_secret: Path = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    platform: YouTubePlatform = YouTubePlatform(client_secret, tmp_path, request_pause_sec=0, rng=random.Random(RNG_SEED))
    forced: list[bool] = []

    def _load(client_secret_file: Path, token_file: Path, login_hint: str, force_reauth: bool = False,
              on_login: Any = None, allow_login: bool = True) -> object:
        forced.append(force_reauth)
        on_login()
        return _Credentials()

    monkeypatch.setattr(youtube_module, "load_credentials", _load)
    monkeypatch.setattr(youtube_module, "build", lambda *args, **kwargs: _FakeService())
    platform._service(CHANNEL, True)
    platform.drop_login(CHANNEL)
    platform.keep_login(CHANNEL)
    assert not (tmp_path / "@KanalUA.token.json").exists()
    platform._service(CHANNEL, True)                   # клиент забыт: вход заново, мимо токена
    assert forced == [False, True]


def test_operations_other_than_describe_never_open_the_browser(
    platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed: list[bool] = []

    def _load(client_secret_file: Path, token_file: Path, login_hint: str, force_reauth: bool = False,
              on_login: Any = None, allow_login: bool = True) -> object:
        allowed.append(allow_login)
        raise AuthError(AuthErrorReason.LOGIN_REQUIRED, token_file.name)

    monkeypatch.setattr(youtube_module, "load_credentials", _load)
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == ERROR_LOGIN_REQUIRED and allowed == [False]


def test_server_error_is_retried_by_the_policy_then_refused(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    """503: первое обращение и 4 повтора с паузами RetryPolicy (2–3, 4–5, 8–9, 16–17 с), затем отказ."""
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(liveBroadcasts=[_http_error(503, "backendError", "x")] * 5)
    )
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == "transportFailed"
    assert len(service.calls) == 5
    assert clock.sleeps == _expected_delays(4)
    assert [int(delay) for delay in clock.sleeps] == [2, 4, 8, 16]


def test_describe_without_login_is_refused_but_not_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """allow_login=False: нужен вход — loginRequired без браузера; следующий обычный вызов входит как всегда."""
    client_secret: Path = tmp_path / "client_secret.json"
    client_secret.write_text("{}", encoding="utf-8")
    platform: YouTubePlatform = YouTubePlatform(client_secret, tmp_path, request_pause_sec=0, rng=random.Random(RNG_SEED))
    allowed: list[bool] = []

    def _load(
        client_secret_file: Path,
        token_file: Path,
        login_hint: str,
        force_reauth: bool = False,
        on_login: Any = None,
        allow_login: bool = True,
    ) -> object:
        allowed.append(allow_login)
        if not allow_login:
            raise AuthError(AuthErrorReason.LOGIN_REQUIRED, token_file.name)
        return object()

    service: _FakeService = _FakeService(
        channels=[{"items": [{"id": "UC1", "snippet": {"title": "Канал UA", "customUrl": "@kanalua"}}]}]
    )
    monkeypatch.setattr(youtube_module, "load_credentials", _load)
    monkeypatch.setattr(youtube_module, "build", lambda *args, **kwargs: service)
    with pytest.raises(PlatformError) as refused:
        platform.describe_channel(CHANNEL, allow_login=False)
    assert refused.value.code == ERROR_LOGIN_REQUIRED
    info: ChannelInfo = platform.describe_channel(CHANNEL)
    assert (info.youtube_channel_id, info.handle_raw) == ("UC1", "@kanalua")
    assert allowed == [False, True]


def test_unexpected_stream_key_is_an_error(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ключ не того вида — эфир не засчитан: ключ не берём, форму не шлём (§7.4 п.2)."""
    _install(
        platform,
        monkeypatch,
        _FakeService(
            liveBroadcasts=[{"id": "B1"}],
            liveStreams=[_stream_response("STRANGE")],
        ),
    )
    with caplog.at_level("ERROR"), pytest.raises(PlatformError) as raised:
        platform.create_broadcast(CHANNEL, _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)))
    assert raised.value.code == "unexpectedStreamKeyFormat"
    assert "STRANGE" not in caplog.text          # ключ только замаскированным


def test_attach_stream_reuses_the_same_path(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[{"id": "B1"}], liveStreams=[_stream_response()]),
    )
    created: CreatedBroadcast = platform.attach_stream(
        CHANNEL,
        "B1",
        _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)),
    )
    assert created.stream_key == GOOD_KEY
    assert [call["resource"] for call in service.calls] == ["liveStreams", "liveBroadcasts"]
    assert service.calls[1]["method"] == "bind"


def test_update_sends_time_and_category(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update заменяет snippet целиком: без времени и категории они бы потерялись."""
    service: _FakeService = _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"id": "B1"}]))
    spec: BroadcastSpec = _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc))
    platform.update_broadcast(CHANNEL, "B1", spec)
    body: dict[str, Any] = service.calls[0]["body"]
    assert service.calls[0]["part"] == "snippet"      # contentDetails тянет monitorStream
    assert body["id"] == "B1"
    assert body["snippet"]["scheduledStartTime"] == "2027-03-17T17:00:00Z"
    assert body["snippet"]["categoryId"] == spec.category_id   # категория из спеки, а не найденная


def test_set_stream_marker_rewrites_snippet_whole(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """liveStreams.update(part=snippet): читаем snippet целиком, меняем название и описание, пишем обратно."""
    stream_snippet: dict[str, Any] = {"title": "Мой ключ", "description": "", "isDefaultStream": False, "channelId": "UC1"}
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveStreams=[{"items": [{"id": "S1", "snippet": stream_snippet}]}, {"id": "S1"}]),
    )
    platform.set_stream_marker(CHANNEL, "S1", "17-03-2027_1900_uk")
    assert [(call["method"], call["part"]) for call in service.calls] == [("list", "snippet"), ("update", "snippet")]
    body: dict[str, Any] = service.calls[1]["body"]
    assert body["id"] == "S1"
    assert body["snippet"]["title"] == "17-03-2027_1900_uk"
    assert body["snippet"]["description"].startswith("Ключ планера: канал «Канал UA» @KanalUA, эфир 17-03-2027 19:00, язык uk")
    assert (body["snippet"]["channelId"], body["snippet"]["isDefaultStream"]) == ("UC1", False)   # не затёрты


def _video_item(**overrides: Any) -> dict[str, Any]:
    snippet: dict[str, Any] = {"title": "Эфир", "description": "Описание", "categoryId": "22",
                               "defaultLanguage": "uk", "defaultAudioLanguage": "uk"}
    status: dict[str, Any] = {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False}
    snippet.update(overrides.get("snippet", {}))
    status.update(overrides.get("status", {}))
    return {"items": [{"id": "B1", "snippet": snippet, "status": status}]}


def test_video_settings_are_applied_in_one_write(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один list и один update: язык, категория и аудитория правятся вместе."""
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[
                _video_item(snippet={"defaultLanguage": "en", "categoryId": "24"},
                            status={"selfDeclaredMadeForKids": True}),
                {"id": "B1"},
            ]
        ),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert (fixes.language_set, fixes.category_set, fixes.audience_cleared) == (True, True, True)
    assert fixes.privacy_set is False
    assert len(service.calls) == 2
    body: dict[str, Any] = service.calls[1]["body"]
    assert service.calls[1]["part"] == "snippet,status"
    assert body["snippet"]["defaultLanguage"] == "uk" and body["snippet"]["defaultAudioLanguage"] == "uk"
    assert body["snippet"]["categoryId"] == "22"
    assert body["snippet"]["title"] == "Эфир"                   # непереданное поле не теряется
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["privacyStatus"] == "unlisted"        # частичный status затёр бы его


def test_write_response_says_what_the_platform_stored(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ответ videos.update — источник записанного: перечитывание сразу после записи отстаёт."""
    _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[
                _video_item(snippet={"defaultLanguage": "ru", "defaultAudioLanguage": "ru"}),
                {
                    "id": "B1",
                    "snippet": {"defaultLanguage": "uk", "defaultAudioLanguage": "uk", "categoryId": "22"},
                    "status": {"privacyStatus": "unlisted", "madeForKids": False},
                },
            ]
        ),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert fixes.language_set is True and fixes.applied is not None
    assert (fixes.applied.language, fixes.applied.audio_language) == ("uk", "uk")
    assert (fixes.applied.category_id, fixes.applied.privacy) == ("22", "unlisted")
    assert fixes.applied.made_for_kids is False


def test_empty_write_response_leaves_the_read_value(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Площадка ничего не прислала в ответе — подставлять нечего, перечитанное остаётся как есть."""
    _install(
        platform,
        monkeypatch,
        _FakeService(videos=[_video_item(snippet={"defaultLanguage": "ru"}), {"id": "B1"}]),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert fixes.applied is not None and fixes.applied.language is None
    stale: BroadcastFacts = _facts(default_language="ru")
    assert fixes.apply_to_facts(stale).default_language == "ru"


def test_applied_fields_replace_the_reread_ones(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Записанное этим запуском берётся из ответа записи; поле, которое не писали, не трогается."""
    _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[
                _video_item(snippet={"defaultLanguage": "ru", "defaultAudioLanguage": "ru"}),
                {
                    "id": "B1",
                    "snippet": {"defaultLanguage": "uk", "defaultAudioLanguage": "uk", "categoryId": "22"},
                    "status": {"privacyStatus": "unlisted"},
                },
            ]
        ),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    facts: BroadcastFacts = fixes.apply_to_facts(_facts(default_language="ru", title="Старое название"))
    assert (facts.default_language, facts.default_audio_language) == ("uk", "uk")
    assert facts.title == "Старое название"        # запись названия сюда не входит


def _facts(**overrides: Any) -> BroadcastFacts:
    """Снимок фактов эфира: минимум обязательных полей, остальное — по месту теста."""
    fields: dict[str, Any] = {
        "broadcast_id": "B1",
        "title": "Эфир",
        "description": "Описание",
        "start_utc": None,
        "privacy_status": "unlisted",
        "made_for_kids": False,
        "age_restricted": False,
        "default_language": "uk",
        "default_audio_language": "uk",
        "category_id": "22",
        "bound_stream_id": "S1",
        "stream_marker": "17-03-2027_1900_uk",
    }
    return BroadcastFacts(**{**fields, **overrides})


def test_matching_video_settings_are_not_written(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Совпало всё — записи не делается вовсе: лишняя квота и лишний риск."""
    service: _FakeService = _install(platform, monkeypatch, _FakeService(videos=[_video_item()]))
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert fixes.any_fix is False
    assert len(service.calls) == 1


def test_channel_level_audience_is_also_fixed(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """madeForKids=true при selfDeclared=false — это аудитория, выставленная на весь канал."""
    _install(
        platform,
        monkeypatch,
        _FakeService(videos=[_video_item(status={"madeForKids": True}), {"id": "B1"}]),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert fixes.audience_cleared is True


def test_privacy_is_restored_in_the_same_write(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Владелец поставил Private: видимость возвращается тем же videos.update, status — целиком."""
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(videos=[_video_item(status={"privacyStatus": "private", "embeddable": True}), {"id": "B1"}]),
    )
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22", "unlisted")
    assert (fixes.privacy_set, fixes.language_set, fixes.category_set) == (True, False, False)
    body: dict[str, Any] = service.calls[1]["body"]
    assert body["status"]["privacyStatus"] == "unlisted"
    assert body["status"]["embeddable"] is True

def test_read_facts_collects_language_audience_and_age(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Язык, аудитория и возраст видны только у videos, потока — у самого эфира."""
    _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[
                {
                    "items": [
                        {
                            "id": "B1",
                            "snippet": {"title": "Эфир", "description": "Описание",
                                        "defaultLanguage": "ru", "defaultAudioLanguage": "ru",
                                        "categoryId": "22"},
                            "status": {"privacyStatus": "unlisted", "madeForKids": False},
                            "contentDetails": {"contentRating": {"ytRating": "ytAgeRestricted"}},
                            # время старта у videos живёт здесь, а не в snippet
                            "liveStreamingDetails": {"scheduledStartTime": "2027-03-17T17:00:00Z"},
                        }
                    ]
                }
            ],
            liveBroadcasts=[{"items": [_broadcast_item("B1", "2027-03-17T17:00:00Z")]}],
            liveStreams=[
                {
                    "items": [
                        {"id": "S1", "snippet": {"title": "17-03-2027_1900_uk"},
                         "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://x", "streamName": GOOD_KEY}}}
                    ]
                }
            ],
        ),
    )
    facts: BroadcastFacts = platform.read_facts(CHANNEL, "B1")
    assert (facts.default_language, facts.default_audio_language) == ("ru", "ru")
    assert facts.made_for_kids is False
    assert facts.age_restricted is True
    assert (facts.privacy_status, facts.category_id) == ("unlisted", "22")
    assert (facts.bound_stream_id, facts.stream_marker) == ("S1", "17-03-2027_1900_uk")
    assert facts.start_utc == datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)


def test_read_facts_asks_for_live_streaming_details() -> None:
    """Живой прогон 13-09-2026 01:21: без этой части время старта приходило пустым."""
    assert "liveStreamingDetails" in youtube_module.VIDEO_FACTS_PARTS


def test_read_facts_without_live_streaming_details_gives_none(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """У эфира без запланированного времени его действительно нет — это не ошибка."""
    _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[{"items": [{"id": "B1", "snippet": {"title": "Эфир", "description": ""},
                                "status": {}, "contentDetails": {}}]}],
            liveBroadcasts=[{"items": []}],
        ),
    )
    facts: BroadcastFacts = platform.read_facts(CHANNEL, "B1")
    assert facts.start_utc is None
    assert facts.bound_stream_id is None


def test_facts_take_chat_and_largest_thumbnail(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """liveChatId живёт у эфира, обложка — у видео; берётся самое крупное разрешение."""
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    item["snippet"]["liveChatId"] = "CHAT1"
    _install(
        platform,
        monkeypatch,
        _FakeService(
            videos=[
                {
                    "items": [
                        {
                            "id": "B1",
                            "snippet": {"title": "Эфир", "description": "",
                                        "thumbnails": {"default": {"url": "small.jpg", "width": 120},
                                                       "maxres": {"url": "big.jpg", "width": 1280}}},
                            "status": {},
                            "contentDetails": {},
                            "liveStreamingDetails": {"scheduledStartTime": "2027-03-17T17:00:00Z"},
                        }
                    ]
                }
            ],
            liveBroadcasts=[{"items": [item]}],
            liveStreams=[
                {"items": [{"id": "S1", "snippet": {"title": "17-03-2027_1900_uk"},
                            "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://x",
                                                      "streamName": GOOD_KEY}}}]}
            ],
        ),
    )
    facts: BroadcastFacts = platform.read_facts(CHANNEL, "B1")
    assert facts.live_chat_id == "CHAT1"
    assert facts.thumbnail_url == "big.jpg"


def test_broadcast_list_reads_live_chat_id(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    item["snippet"]["liveChatId"] = "CHAT1"
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [item]}]))
    [broadcast] = platform.list_upcoming(CHANNEL)
    assert broadcast.live_chat_id == "CHAT1"


# --- задача 5k: картинка эфира и отпечаток заглушки

PICTURE: bytes = b"placeholder-jpeg"
PICTURE_URL: str = "https://i.ytimg.com/vi/B1/default_live.jpg"


class _PictureResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code: int = status_code
        self.content: bytes = content


class _FakeSession:
    """requests.Session для картинок: ответ по адресу, момент каждого скачивания — по часам теста."""

    def __init__(self, pictures: dict[str, Any], clock: _FakeClock | None = None) -> None:
        self._pictures: dict[str, Any] = pictures
        self._clock: _FakeClock | None = clock
        self.calls: list[tuple[str, float | None]] = []

    def get(self, url: str, timeout: int) -> _PictureResponse:
        self.calls.append((url, self._clock.now if self._clock is not None else None))
        answer: Any = self._pictures.get(url, _PictureResponse(404, b""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def _thumbnails(url: str = PICTURE_URL) -> dict[str, Any]:
    return {"default": {"url": url, "width": 120}, "high": {"url": url.replace("default", "hq"), "width": 480}}


def _install_pictures(platform: YouTubePlatform, session: _FakeSession) -> _FakeSession:
    platform._session = session   # type: ignore[assignment]
    return session


def _insert_response(thumbnails: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": "B1", "snippet": {"thumbnails": thumbnails or {}}}


def test_create_writes_placeholder_token_into_stream_description(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[_insert_response(_thumbnails()), {"id": "B1"}], liveStreams=[_stream_response()]),
    )
    _install_pictures(platform, _FakeSession({PICTURE_URL: _PictureResponse(200, PICTURE)}))
    platform.create_broadcast(CHANNEL, _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)))
    description: str = service.calls[1]["body"]["snippet"]["description"]
    assert description.endswith("; заглушка обложки thumb0=" + picture_sha(PICTURE))
    assert placeholder_sha_from_description(description) == picture_sha(PICTURE)


@pytest.mark.parametrize(
    "answer",
    [_PictureResponse(404, b""), _PictureResponse(200, b""), requests.ConnectionError("нет сети")],
    ids=["not_found", "empty_body", "network"],
)
def test_create_without_picture_writes_no_token_and_does_not_fail(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    answer: Any,
) -> None:
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[_insert_response(_thumbnails()), {"id": "B1"}], liveStreams=[_stream_response()]),
    )
    _install_pictures(platform, _FakeSession({PICTURE_URL: answer}))
    created: CreatedBroadcast = platform.create_broadcast(CHANNEL, _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)))
    assert created.stream_key == GOOD_KEY
    assert placeholder_sha_from_description(service.calls[1]["body"]["snippet"]["description"]) is None


def test_attach_stream_does_not_take_a_placeholder(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """На картинке существующего эфира может быть обложка: отпечаток не снимается и не скачивается."""
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(liveStreams=[_stream_response()], liveBroadcasts=[{"id": "B1"}])
    )
    session: _FakeSession = _install_pictures(platform, _FakeSession({}))
    platform.attach_stream(CHANNEL, "B1", _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)))
    assert session.calls == []
    assert placeholder_sha_from_description(service.calls[0]["body"]["snippet"]["description"]) is None


def test_set_stream_marker_keeps_the_placeholder_token(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    old: str = "Ключ планера: старый; заглушка обложки thumb0=044eb0835668"
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveStreams=[{"items": [{"id": "S1", "snippet": {"title": "x", "description": old}}]}, {"id": "S1"}]),
    )
    platform.set_stream_marker(CHANNEL, "S1", "17-03-2027_1900_uk")
    description: str = service.calls[1]["body"]["snippet"]["description"]
    assert description.startswith("Ключ планера: канал «Канал UA» @KanalUA, эфир 17-03-2027 19:00")
    assert placeholder_sha_from_description(description) == "044eb0835668"


def test_get_stream_returns_description(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    item: dict[str, Any] = {
        "id": "S1",
        "snippet": {"title": "m", "description": "Ключ планера; заглушка обложки thumb0=044eb0835668"},
        "cdn": {"ingestionInfo": {"ingestionAddress": "rtmp://a", "streamName": GOOD_KEY}},
    }
    _install(platform, monkeypatch, _FakeService(liveStreams=[{"items": [item]}]))
    stream: StreamInfo | None = platform.get_stream(CHANNEL, "S1")
    assert stream is not None and placeholder_sha_from_description(stream.description) == "044eb0835668"


def test_list_upcoming_takes_picture_sha_and_survives_a_failed_download(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    good["snippet"]["thumbnails"] = _thumbnails()
    broken: dict[str, Any] = _broadcast_item("B2", "2027-03-18T17:00:00Z", stream_id="S2")
    broken["snippet"]["thumbnails"] = _thumbnails("https://i.ytimg.com/vi/B2/default_live.jpg")
    bare: dict[str, Any] = _broadcast_item("B3", "2027-03-19T17:00:00Z", stream_id="S3")   # картинок в ответе нет
    _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [good, broken, bare]}]))
    session: _FakeSession = _install_pictures(
        platform,
        _FakeSession({PICTURE_URL: _PictureResponse(200, PICTURE), broken["snippet"]["thumbnails"]["default"]["url"]:
                      requests.Timeout("долго")}),
    )
    broadcasts: list[UpcomingBroadcast] = platform.list_upcoming(CHANNEL)
    assert [broadcast.thumbnail_sha for broadcast in broadcasts] == [picture_sha(PICTURE), None, None]
    assert [url for url, _ in session.calls] == [PICTURE_URL, broken["snippet"]["thumbnails"]["default"]["url"]]


def test_facts_do_not_download_pictures(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z", stream_id=None)
    item["snippet"]["thumbnails"] = _thumbnails()
    video: dict[str, Any] = {"id": "B1", "snippet": {"title": "t", "description": "d", "thumbnails": _thumbnails()}}
    _install(platform, monkeypatch, _FakeService(videos=[{"items": [video]}], liveBroadcasts=[{"items": [item]}]))
    session: _FakeSession = _install_pictures(platform, _FakeSession({}))
    platform.read_facts(CHANNEL, "B1")
    assert session.calls == []


def test_pause_also_separates_picture_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    platform: YouTubePlatform = _platform_with_pause(tmp_path, 2)
    items: list[dict[str, Any]] = []
    pictures: dict[str, Any] = {}
    for number in (1, 2):
        item: dict[str, Any] = _broadcast_item(f"B{number}", f"2027-03-1{number}T17:00:00Z")
        url: str = f"https://i.ytimg.com/vi/B{number}/default_live.jpg"
        item["snippet"]["thumbnails"] = _thumbnails(url)
        pictures[url] = _PictureResponse(200, PICTURE + bytes([number]))
        items.append(item)
    service: _FakeService = _install(platform, monkeypatch, _FakeService(clock, liveBroadcasts=[{"items": items}]))
    session: _FakeSession = _install_pictures(platform, _FakeSession(pictures, clock))
    platform.list_upcoming(CHANNEL)
    moments: list[float] = [service.calls[0]["at"]] + [moment for _, moment in session.calls if moment is not None]
    assert len(moments) == 3
    assert all(later - earlier >= 2 for earlier, later in zip(moments, moments[1:]))


def test_refusals_of_channels_with_one_title_do_not_mix(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    """Память отказов — по ключу канала (нику): одинаковое название другого канала отказ не наследует."""
    twin: ChannelConfig = replace(CHANNEL, handle="@KanalUA2")
    assert twin.account_name == CHANNEL.account_name
    service: _FakeService = _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[_http_error(401, "authError", "токен отозван"), {"items": []}]),
    )
    with pytest.raises(PlatformError, match="authError"):
        platform.list_upcoming(CHANNEL)
    assert platform.list_upcoming(twin) == []
    assert len(service.calls) == 2


def test_list_upcoming_reads_published_at(platform: YouTubePlatform, monkeypatch: pytest.MonkeyPatch) -> None:
    """snippet.publishedAt уже приходит в part=snippet: время создания эфира без нового вызова."""
    item: dict[str, Any] = _broadcast_item("B1", "2027-03-17T17:00:00Z")
    item["snippet"]["publishedAt"] = "2026-09-16T12:34:56Z"
    bare: dict[str, Any] = _broadcast_item("B2", "2027-03-18T17:00:00Z")
    service: _FakeService = _install(platform, monkeypatch, _FakeService(liveBroadcasts=[{"items": [item, bare]}]))
    first, second = platform.list_upcoming(CHANNEL)
    assert first.published_utc == datetime(2026, 9, 16, 12, 34, 56, tzinfo=timezone.utc)
    assert second.published_utc is None
    assert len(service.calls) == 1


def test_thumbnail_refusal_is_remembered_without_network(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    clock: _FakeClock,
) -> None:
    service: _FakeService = _install(
        platform, monkeypatch, _FakeService(thumbnails=[_http_error(429, "uploadRateLimitExceeded", "лимит")])
    )
    assert platform.thumbnail_refusal(CHANNEL) is None
    with pytest.raises(PlatformError):
        platform.set_thumbnail(CHANNEL, "B1", b"jpg")
    refusal: PlatformError | None = platform.thumbnail_refusal(CHANNEL)
    assert refusal is not None and refusal.code == "uploadRateLimitExceeded"
    assert platform.thumbnail_refusal(OTHER_CHANNEL) is None
    assert len(service.calls) == 1                            # вопрос об отказе к сети не ходит
