from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from app.config.loader import ChannelConfig, Platform, Privacy
from app.google import api_retry
from app.platforms import youtube as youtube_module
from app.pipeline.plan import BroadcastSpec
from app.platforms.base import (
    BroadcastFacts,
    ChannelInfo,
    CreatedBroadcast,
    PlatformError,
    StreamInfo,
    UpcomingBroadcast,
    VideoFixes,
)
from app.platforms.youtube import YOUTUBE_STREAM_KEY_PATTERN, YouTubePlatform

CHANNEL: ChannelConfig = ChannelConfig(
    id="yt_ua",
    platform=Platform.YOUTUBE,
    account_name="Test UA",
    languages=("uk",),
    privacy=Privacy.PUBLIC,
    auto_start=True,
    set_thumbnail=True,
        category_id="22",
)
GOOD_KEY: str = "abcd-1234-efgh-5678-ijkl"


class _FakeRequest:
    def __init__(self, response: Any) -> None:
        self._response: Any = response

    def execute(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeResource:
    """Ресурс googleapiclient: .list(**kwargs) → объект с .execute()."""

    def __init__(self, responses: list[Any], calls: list[dict[str, Any]], name: str) -> None:
        self._responses: list[Any] = responses
        self._calls: list[dict[str, Any]] = calls
        self._name: str = name

    def list(self, **kwargs: Any) -> _FakeRequest:
        return self._call("list", **kwargs)

    def __getattr__(self, method: str) -> Any:
        """insert, bind, update, set — все ведут себя одинаково: очередь ответов."""
        if method.startswith("_"):
            raise AttributeError(method)
        return lambda **kwargs: self._call(method, **kwargs)

    def _call(self, method: str, **kwargs: Any) -> _FakeRequest:
        self._calls.append({"resource": self._name, "method": method, **kwargs})
        if not self._responses:
            raise AssertionError(f"неожиданный вызов {self._name}.{method}")
        return _FakeRequest(self._responses.pop(0))


class _FakeService:
    def __init__(self, **responses: list[Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._resources: dict[str, _FakeResource] = {
            name: _FakeResource(list(items), self.calls, name) for name, items in responses.items()
        }

    def _resource(self, name: str) -> _FakeResource:
        return self._resources.setdefault(name, _FakeResource([], self.calls, name))

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
    return YouTubePlatform(client_secret, tmp_path)


def _install(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    service: _FakeService,
) -> _FakeService:
    """Готовый клиент вместо build() и OAuth: сеть в тестах запрещена."""
    monkeypatch.setattr(platform, "_service", lambda channel: service)
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
    # дефолтный эфир канала без времени старта — норма, поэтому debug, а не warning
    assert "broadcast_without_start" in caplog.text


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
) -> None:
    monkeypatch.setattr(api_retry.time, "sleep", lambda seconds: None)   # без пауз между попытками
    monkeypatch.setattr(youtube_module.time, "sleep", lambda seconds: None)
    attempts: int = youtube_module.RETRY_MAX_ATTEMPTS * youtube_module.RETRY_MAX_ATTEMPTS
    _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[ConnectionError("нет сети")] * attempts),
    )
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == "transportFailed"


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
    assert insert["snippet"]["categoryId"] == CHANNEL.category_id
    assert insert["status"]["privacyStatus"] == "public"
    assert insert["status"]["selfDeclaredMadeForKids"] is False
    assert insert["contentDetails"] == {
        "enableAutoStart": True,
        "enableAutoStop": True,
        "latencyPreference": "normal",
    }
    stream_body: dict[str, Any] = service.calls[1]["body"]
    assert stream_body["snippet"]["title"] == spec.marker      # маркер планера (§7.3)
    assert stream_body["cdn"] == {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"}
    assert service.calls[2]["method"] == "bind"
    assert (service.calls[2]["id"], service.calls[2]["streamId"]) == ("B1", "S1")


def test_unexpected_stream_key_is_an_error(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ключ не того вида — эфир не засчитан: в журнал не пишем, форму не шлём (§7.4 п.2)."""
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
    platform.update_broadcast(CHANNEL, "B1", _spec(datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)))
    body: dict[str, Any] = service.calls[0]["body"]
    assert service.calls[0]["part"] == "snippet"      # contentDetails тянет monitorStream
    assert body["id"] == "B1"
    assert body["snippet"]["scheduledStartTime"] == "2027-03-17T17:00:00Z"
    assert body["snippet"]["categoryId"] == CHANNEL.category_id   # категория канала, а не найденная


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
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22")
    assert (fixes.language_set, fixes.category_set, fixes.audience_cleared) == (True, True, True)
    assert len(service.calls) == 2
    body: dict[str, Any] = service.calls[1]["body"]
    assert service.calls[1]["part"] == "snippet,status"
    assert body["snippet"]["defaultLanguage"] == "uk" and body["snippet"]["defaultAudioLanguage"] == "uk"
    assert body["snippet"]["categoryId"] == "22"
    assert body["snippet"]["title"] == "Эфир"                   # непереданное поле не теряется
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["privacyStatus"] == "unlisted"        # частичный status затёр бы его


def test_matching_video_settings_are_not_written(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Совпало всё — записи не делается вовсе: лишняя квота и лишний риск."""
    service: _FakeService = _install(platform, monkeypatch, _FakeService(videos=[_video_item()]))
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22")
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
    fixes: VideoFixes = platform.apply_video_settings(CHANNEL, "B1", "uk", "22")
    assert fixes.audience_cleared is True

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
