from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.records.record_store import SCHEMA_VERSION, RecordStore
from app.records.run_record import RunRecord, RunStore

NOW: datetime = datetime(2026, 9, 21, 9, 49, tzinfo=timezone.utc)


def _record(run_id: str, quota_day: str, units: int) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        started_utc=NOW.isoformat(),
        quota_day=quota_day,
        quota_units=units,
        elapsed_sec=583.4,
        exit_code=0,
        mode="full",
        version="0.1.8",
        stats={"youtube": {"calls": 3}},
    )


def _rows(path: Path, sql: str) -> list[tuple[object, ...]]:
    connection: sqlite3.Connection = sqlite3.connect(path)
    try:
        return list(connection.execute(sql))
    finally:
        connection.close()


def test_runs_of_one_quota_day_are_summed(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    store: RunStore = RunStore(path)
    assert store.save(_record("20-09-2026_232200", "20-09-2026", 9600)) == 9600
    assert store.save(_record("21-09-2026_092900", "21-09-2026", 230)) == 230      # другие сутки — не в сумме
    assert store.save(_record("21-09-2026_094926", "21-09-2026", 240)) == 470
    rows = _rows(path, "SELECT run_id, quota_units, mode, version FROM runs ORDER BY started_utc, run_id")
    assert [row[0] for row in rows] == ["20-09-2026_232200", "21-09-2026_092900", "21-09-2026_094926"]
    assert _rows(path, "SELECT stats_json FROM runs WHERE run_id = '21-09-2026_094926'") == [('{"youtube": {"calls": 3}}',)]


def test_runs_do_not_touch_slots_and_raise_schema_version(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    RecordStore.open(path, read_only=False, now_local=NOW).close()
    connection: sqlite3.Connection = sqlite3.connect(path)
    with connection:
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")   # база прежней версии
        connection.execute(
            "INSERT INTO slots VALUES ('24-09-2026_1900_ru', 'UC1', '2026-09-24T16:00:00+00:00',"
            " 'key_confirmed', '20-09-2026 23:40', '{}')"
        )
    connection.close()
    assert RunStore(path).save(_record("21-09-2026_094926", "21-09-2026", 10)) == 10
    assert _rows(path, "SELECT value FROM meta WHERE key = 'schema_version'") == [(SCHEMA_VERSION,)]
    assert _rows(path, "SELECT slot_id FROM slots") == [("24-09-2026_1900_ru",)]
    assert SCHEMA_VERSION == "2"


def test_existing_memory_gets_schema_version_two_on_open(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    RecordStore.open(path, read_only=False, now_local=NOW).close()
    connection: sqlite3.Connection = sqlite3.connect(path)
    with connection:
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.close()
    RecordStore.open(path, read_only=False, now_local=NOW).close()
    assert _rows(path, "SELECT value FROM meta WHERE key = 'schema_version'") == [("2",)]


def test_failed_write_is_a_warning_and_no_data(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path: Path = tmp_path / "planer.sqlite3"
    path.mkdir()          # на месте файла — папка: sqlite не откроет
    with caplog.at_level("WARNING", logger="planer"):
        assert RunStore(path).save(_record("21-09-2026_094926", "21-09-2026", 10)) is None
    assert any(message.startswith("run_stats_write_failed") for message in caplog.messages)
