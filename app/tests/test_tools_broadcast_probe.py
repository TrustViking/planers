"""Пробник эфиров: выгрузка сырых элементов эфиров без времени старта (задача 5n-B)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.paths import PlanerPaths, build_paths
from app.tools.broadcast_probe import _list_raw_items, _raw_start, _write_undated


class _Request:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response: dict[str, Any] = response

    def execute(self) -> dict[str, Any]:
        return self._response


class _Broadcasts:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages: list[dict[str, Any]] = pages
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(kwargs)
        return _Request(self._pages.pop(0))


class _Service:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.broadcasts: _Broadcasts = _Broadcasts(pages)

    def liveBroadcasts(self) -> _Broadcasts:  # noqa: N802 — имя как у googleapiclient
        return self.broadcasts


def test_undated_items_are_dumped_raw_with_stream_keys_masked(tmp_path: Path) -> None:
    undated: dict[str, Any] = {
        "id": "40S-8MpatXI",
        "snippet": {"title": "Прямая трансляция"},
        "status": {"lifeCycleStatus": "ready"},
        "cdn": {"ingestionInfo": {"streamName": "abcd-efgh-ijkl-mnop"}},
    }
    dated: dict[str, Any] = {"id": "B2", "snippet": {"scheduledStartTime": "2027-03-17T17:00:00Z"}}
    service: _Service = _Service([{"items": [dated], "nextPageToken": "p2"}, {"items": [undated]}])
    items: list[dict[str, Any]] = _list_raw_items(service)
    assert "broadcastType" not in service.broadcasts.calls[0]          # тот же запрос, что у планера
    paths: PlanerPaths = build_paths(tmp_path)
    paths.logs_dir.mkdir(parents=True)
    [written] = [_write_undated(paths, "18-09-2026_160000", item) for item in items if not _raw_start(item)]
    assert written.name == "18-09-2026_160000_undated_broadcast_40S-8MpatXI.json"
    saved: dict[str, Any] = json.loads(written.read_text(encoding="utf-8"))
    assert saved["snippet"] == undated["snippet"] and saved["status"] == undated["status"]
    assert saved["cdn"]["ingestionInfo"]["streamName"] == "****-mnop"
