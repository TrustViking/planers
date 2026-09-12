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
from app.platforms.base import ChannelInfo, PlatformError, StreamInfo, UpcomingBroadcast
from app.platforms.youtube import YOUTUBE_STREAM_KEY_PATTERN, YouTubePlatform

CHANNEL: ChannelConfig = ChannelConfig(
    id="yt_ua",
    platform=Platform.YOUTUBE,
    account_name="Test UA",
    languages=("uk",),
    privacy=Privacy.PUBLIC,
    auto_start=True,
    set_thumbnail=True,
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
        self._calls.append({"resource": self._name, **kwargs})
        if not self._responses:
            raise AssertionError(f"неожиданный вызов {self._name}.list")
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
    with caplog.at_level("WARNING"):
        assert platform.list_upcoming(CHANNEL) == []
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
    _install(
        platform,
        monkeypatch,
        _FakeService(liveBroadcasts=[ConnectionError("нет сети")] * youtube_module.RETRY_MAX_ATTEMPTS),
    )
    with pytest.raises(PlatformError) as raised:
        platform.list_upcoming(CHANNEL)
    assert raised.value.code == "transportFailed"


def test_create_and_update_are_not_implemented_yet(
    platform: YouTubePlatform,
    monkeypatch: pytest.MonkeyPatch,
    make_slot_object: Any,
) -> None:
    slot: Any = make_slot_object(datetime(2027, 3, 17, 19, 0, tzinfo=timezone.utc), "uk")
    with pytest.raises(PlatformError) as created:
        platform.create_broadcast(CHANNEL, slot, None)
    with pytest.raises(PlatformError) as updated:
        platform.update_broadcast(CHANNEL, "B1", slot, None)
    assert created.value.code == "notImplementedYet"
    assert updated.value.code == "notImplementedYet"
