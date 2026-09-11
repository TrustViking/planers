from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.state.registry import FormStatus, PackageSeen, Registration, Registry, RegistryError

KEY: str = "16-09-2026_1900_uk|yt_ua"
FIELD_ORDER: list[str] = [
    "slot_id",
    "channel_id",
    "account_name",
    "language",
    "date",
    "time",
    "broadcast_id",
    "broadcast_url",
    "stream_url",
    "stream_key",
    "package_id",
    "created_at",
    "form_status",
    "form_sent_at",
    "previous_broadcast_ids",
    "last_error",
]


def _registration(**overrides: Any) -> Registration:
    values: dict[str, Any] = dict(
        slot_id="16-09-2026_1900_uk",
        channel_id="yt_ua",
        account_name="Іван UA",
        language="uk",
        date="16-09-2026",
        time="19:00",
        broadcast_id="abc123",
        broadcast_url="https://www.youtube.com/watch?v=abc123",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="xxxx-xxxx-xxxx-xxxx-xxxx",
        package_id="20260913-101502-a1b2c3",
        created_at=datetime(2026, 9, 13, 12, 0),
        form_status=FormStatus.SENT,
        form_sent_at=datetime(2026, 9, 13, 12, 0),
        previous_broadcast_ids=[],
        last_error=None,
    )
    values.update(overrides)
    return Registration(**values)


def _filled_registry() -> Registry:
    registry: Registry = Registry()
    registry.upsert(_registration())
    registry.note_package(
        "20260913-101502-a1b2c3",
        "plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast",
        datetime(2026, 9, 13, 12, 0),
    )
    return registry


def test_missing_file_gives_empty_registry(tmp_path: Path) -> None:
    registry: Registry = Registry.load(tmp_path / "registry.json")
    assert dict(registry.registrations) == {}
    assert dict(registry.packages) == {}


def test_key_format() -> None:
    assert Registry.key("16-09-2026_1900_uk", "yt_ua") == KEY


def test_save_load_round_trip_is_byte_stable(tmp_path: Path) -> None:
    first: Path = tmp_path / "registry.json"
    second: Path = tmp_path / "copy" / "registry.json"
    _filled_registry().save(first)
    loaded: Registry = Registry.load(first)
    loaded.save(second)
    assert first.read_bytes() == second.read_bytes()
    assert loaded.get(KEY) == _registration()
    payload: dict[str, Any] = json.loads(first.read_text(encoding="utf-8"))
    assert list(payload["registrations"][KEY]) == FIELD_ORDER
    assert payload["registrations"][KEY]["created_at"] == "13-09-2026 12:00"
    assert "Іван UA" in first.read_text(encoding="utf-8")


def test_save_leaves_no_temp_file(tmp_path: Path) -> None:
    path: Path = tmp_path / "state" / "registry.json"
    _filled_registry().save(path)
    assert [item.name for item in path.parent.iterdir()] == ["registry.json"]


def test_mark_form_sent(tmp_path: Path) -> None:
    registry: Registry = Registry()
    registry.upsert(_registration(form_status=FormStatus.PENDING, form_sent_at=None))
    registry.mark_form_sent(KEY, datetime(2026, 9, 14, 8, 30))
    registration: Registration | None = registry.get(KEY)
    assert registration is not None
    assert (registration.form_status, registration.form_sent_at) == (FormStatus.SENT, datetime(2026, 9, 14, 8, 30))


def test_mark_recreated_keeps_history_and_resets_form() -> None:
    registry: Registry = _filled_registry()
    registry.mark_recreated(
        KEY,
        broadcast_id="new456",
        broadcast_url="https://www.youtube.com/watch?v=new456",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="yyyy-yyyy-yyyy-yyyy-yyyy",
        created_at=datetime(2026, 9, 15, 9, 0),
    )
    registration: Registration | None = registry.get(KEY)
    assert registration is not None
    assert registration.broadcast_id == "new456"
    assert registration.previous_broadcast_ids == ["abc123"]
    assert registration.form_status is FormStatus.PENDING
    assert registration.form_sent_at is None
    assert registration.created_at == datetime(2026, 9, 15, 9, 0)


def test_note_package_keeps_first_seen() -> None:
    registry: Registry = _filled_registry()
    registry.note_package("20260913-101502-a1b2c3", "renamed.bcast", datetime(2026, 9, 20, 10, 0))
    assert registry.packages["20260913-101502-a1b2c3"] == PackageSeen(
        file="plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast",
        first_seen=datetime(2026, 9, 13, 12, 0),
    )


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    path: Path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema_version": 2, "registrations": {}, "packages": {}}), encoding="utf-8")
    with pytest.raises(RegistryError):
        Registry.load(path)


def test_mark_unknown_key_is_rejected() -> None:
    with pytest.raises(RegistryError):
        Registry().mark_form_sent(KEY, datetime(2026, 9, 14, 8, 30))
