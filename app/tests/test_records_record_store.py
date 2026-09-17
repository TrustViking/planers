from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.records import record_store as store_module
from app.records.record_store import RecordStore
from app.records.slot_record import RecordResults, SlotRecord, SlotStage
from app.ui import messages_ru as msg

NOW: datetime = datetime(2027, 3, 16, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def _record(slot_id: str = "17-03-2027_1900_uk", start: str = "2027-03-17T17:00:00+00:00") -> SlotRecord:
    return SlotRecord(
        slot_id=slot_id,
        youtube_channel_id="UC1",
        slot_start_utc=start,
        stage=SlotStage.PUBLISHED,
        updated_at="16-03-2027 12:00",
        results=RecordResults(stream_key="abcd-abcd-abcd-abcd-abcd"),
    )


def test_new_file_is_new_then_existing_is_not(tmp_path: Path) -> None:
    path: Path = tmp_path / "secrets" / "planer.sqlite3"
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    assert store.is_new and path.exists()
    store.close()
    again: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    assert not again.is_new
    again.close()


def test_upsert_find_and_keys_in_full(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    assert store.find("17-03-2027_1900_uk", "UC1") is None
    assert store.save(_record())
    updated: SlotRecord = replace(_record(), stage=SlotStage.KEY_CONFIRMED)
    assert store.save(updated)
    store.close()
    reopened: RecordStore = RecordStore.open(path, read_only=True, now_local=NOW)
    found: SlotRecord | None = reopened.find("17-03-2027_1900_uk", "UC1")
    assert found is not None and found.stage is SlotStage.KEY_CONFIRMED
    assert found.results.stream_key == "abcd-abcd-abcd-abcd-abcd"
    assert reopened.find("17-03-2027_1900_uk", "UC2") is None           # запись — по id канала YouTube
    rows: int = sqlite3.connect(path).execute("SELECT count(*) FROM slots").fetchone()[0]
    assert rows == 1
    reopened.close()


def test_delete_started_before(tmp_path: Path) -> None:
    store: RecordStore = RecordStore.open(tmp_path / "planer.sqlite3", read_only=False, now_local=NOW)
    store.save(_record("15-02-2027_1900_uk", "2027-02-15T17:00:00+00:00"))
    store.save(_record("17-03-2027_1900_uk", "2027-03-17T17:00:00+00:00"))
    assert store.delete_started_before(datetime(2027, 3, 1, tzinfo=timezone.utc)) == 1
    assert store.find("15-02-2027_1900_uk", "UC1") is None
    assert store.find("17-03-2027_1900_uk", "UC1") is not None
    store.close()


def test_broken_file_is_renamed_and_a_new_memory_is_created(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    path.write_bytes(b"this is not a sqlite database at all" * 100)
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    assert store.is_new
    broken: list[Path] = list(tmp_path.glob("planer.sqlite3.broken-*"))
    assert [item.name for item in broken] == [f"planer.sqlite3.broken-{NOW.strftime('%d-%m-%Y_%H%M%S')}"]
    assert broken[0].read_bytes().startswith(b"this is not")
    assert store.save(_record())
    assert store.take_warnings() == [msg.WARNING_RECORDS_BROKEN.format(path=path, renamed=broken[0].name)]
    store.close()


def test_read_only_without_file_writes_nothing(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    store: RecordStore = RecordStore.open(path, read_only=True, now_local=NOW)
    assert store.is_new and store.is_read_only and store.path == path
    assert store.save(_record()) is False
    assert store.find("17-03-2027_1900_uk", "UC1") is None
    assert store.delete_started_before(NOW) == 0
    store.close()
    assert list(tmp_path.iterdir()) == []


def test_read_only_with_file_does_not_change_it(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    writable: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    writable.save(_record())
    writable.close()
    before: bytes = path.read_bytes()
    store: RecordStore = RecordStore.open(path, read_only=True, now_local=NOW)
    assert not store.is_new and store.find("17-03-2027_1900_uk", "UC1") is not None
    assert store.save(replace(_record(), stage=SlotStage.KEY_CONFIRMED)) is False
    store.close()
    assert path.read_bytes() == before


def test_read_only_broken_file_works_without_records(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    path.write_bytes(b"garbage" * 1000)
    store: RecordStore = RecordStore.open(path, read_only=True, now_local=NOW)
    assert store.find("17-03-2027_1900_uk", "UC1") is None
    assert store.take_warnings() == [msg.WARNING_RECORDS_BROKEN_READ_ONLY.format(path=path)]
    store.close()
    assert path.read_bytes() == b"garbage" * 1000 and len(list(tmp_path.iterdir())) == 1


def test_write_failure_does_not_escape_and_is_reported_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store: RecordStore = RecordStore.open(tmp_path / "planer.sqlite3", read_only=False, now_local=NOW)
    store._connection.close()                            # noqa: SLF001 — имитация сбоя базы
    with caplog.at_level("WARNING"):
        assert store.save(_record()) is False
        assert store.save(_record("18-03-2027_1900_uk")) is False
    assert sum(1 for message in caplog.messages if message.startswith("records_write_failed")) == 2
    [warning] = store.take_warnings()
    assert warning.startswith("память планера ") and "не записывается" in warning
    assert store.find("17-03-2027_1900_uk", "UC1") is None               # чтение тоже не падает
    store.close()


def test_close_is_idempotent() -> None:
    store: RecordStore = RecordStore.memory()
    store.close()
    store.close()
    assert store.is_closed


def test_schema_version_is_recorded(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    RecordStore.open(path, read_only=False, now_local=NOW).close()
    value = sqlite3.connect(path).execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert value == (store_module.SCHEMA_VERSION,)


def test_saved_line_names_requested_stage_only_when_it_differs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="planer")
    store: RecordStore = RecordStore.open(tmp_path / "planer.sqlite3", read_only=False, now_local=NOW)
    store.save(_record(), requested=SlotStage.ADMITTED)
    store.save(_record(), requested=SlotStage.PUBLISHED)
    store.close()
    lines: list[str] = [record.getMessage() for record in caplog.records if record.getMessage().startswith("record_saved")]
    assert lines == [
        "record_saved slot_id=17-03-2027_1900_uk youtube_channel_id=UC1 stage=published requested=admitted",
        "record_saved slot_id=17-03-2027_1900_uk youtube_channel_id=UC1 stage=published",
    ]


def test_record_read_back_has_same_content_except_updated_at(tmp_path: Path) -> None:
    """Сравнение «запись изменилась»: прочитанная из базы и та же, построенная заново, — одинаковые."""
    path: Path = tmp_path / "planer.sqlite3"
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    store.save(_record())
    found: SlotRecord | None = store.find("17-03-2027_1900_uk", "UC1")
    store.close()
    assert found is not None
    later: SlotRecord = replace(_record(), updated_at="16-03-2027 13:00")
    assert later.has_same_content(found)
    assert not later.has_same_content(None)
    assert not replace(later, stage=SlotStage.KEY_CONFIRMED).has_same_content(found)
    changed: SlotRecord = replace(later, results=RecordResults(stream_key="bcde-bcde-bcde-bcde-bcde"))
    assert not changed.has_same_content(found)


def test_created_utc_is_written_with_a_new_base_and_kept(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=NOW)
    assert store.created_utc == NOW.astimezone(timezone.utc)
    store.close()
    later: datetime = NOW + timedelta(days=2)
    again: RecordStore = RecordStore.open(path, read_only=False, now_local=later)
    assert again.created_utc == NOW.astimezone(timezone.utc)
    again.close()
    reader: RecordStore = RecordStore.open(path, read_only=True, now_local=later)
    assert reader.created_utc == NOW.astimezone(timezone.utc)
    reader.close()


def test_base_without_created_utc_gets_the_current_time(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """д) База из сборки до 5m-E: строки created_utc нет — при первом открытии на запись пишется «сейчас»."""
    caplog.set_level("INFO", logger="planer")
    path: Path = tmp_path / "planer.sqlite3"
    RecordStore.open(path, read_only=False, now_local=NOW).close()
    connection: sqlite3.Connection = sqlite3.connect(path)
    with connection:
        connection.execute("DELETE FROM meta WHERE key = 'created_utc'")
    connection.close()
    later: datetime = NOW + timedelta(days=2)
    reader: RecordStore = RecordStore.open(path, read_only=True, now_local=later)
    assert reader.created_utc == later.astimezone(timezone.utc)          # только чтение: в базу не пишет
    reader.close()
    store: RecordStore = RecordStore.open(path, read_only=False, now_local=later)
    assert store.created_utc == later.astimezone(timezone.utc) and not store.is_new
    store.close()
    lines: list[str] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("records_created_utc")]
    assert lines == [f"records_created_utc_initialized value={later.astimezone(timezone.utc).isoformat()}"]
    value: str = sqlite3.connect(path).execute("SELECT value FROM meta WHERE key = 'created_utc'").fetchone()[0]
    assert value == later.astimezone(timezone.utc).isoformat()


def test_memory_without_file_uses_the_given_or_current_time() -> None:
    moment: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert RecordStore.memory(created_utc=moment).created_utc == moment
    assert RecordStore.memory().created_utc.tzinfo is not None
