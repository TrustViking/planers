"""Хранилище памяти планера — SQLite (stdlib sqlite3), файл secrets\\planer.sqlite3 (PlanerPaths.records_file).

Таблица slots: одна строка на (slot_id, youtube_channel_id), колонки поиска и record_json (SlotRecord).
Таблица meta: schema_version и created_utc — когда память появилась (ISO-8601 UTC): эфиры с меткой планера,
созданные на площадке раньше, считаются уже переданными стримеру (PlannedBroadcast.bootstrap_confirmation).
У базы без created_utc строка пишется при первом открытии на запись — текущим временем. Сбой базы не валит запуск: не открылась — файл переименовывается
в planer.sqlite3.broken-<DD-MM-YYYY_HHMMSS> (не удаляется) и создаётся новая; не записалось — WARNING
и одна строка предупреждения за запуск, объект работает дальше. read_only (--dry-run, --status) —
на диск ничего не пишется: файла нет — пустая база в памяти.
Ключи потока в базе — полностью (как в keys.txt); в логах — только маской.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from app.core.dates import FILE_STAMP_FORMAT
from app.observability.logging_setup import get_logger
from app.records.slot_record import SlotRecord, SlotStage
from app.ui import messages_ru as msg

LOGGER = get_logger("records")

SCHEMA_VERSION: Final[str] = "1"
MEMORY_DATABASE: Final[str] = ":memory:"
READ_ONLY_URI: Final[str] = "file:{path}?mode=ro"
BROKEN_SUFFIX: Final[str] = ".broken-{stamp}"
SCHEMA_SQL: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS slots ("
    " slot_id TEXT NOT NULL, youtube_channel_id TEXT NOT NULL, slot_start_utc TEXT NOT NULL,"
    " stage TEXT NOT NULL, updated_at TEXT NOT NULL, record_json TEXT NOT NULL,"
    " PRIMARY KEY (slot_id, youtube_channel_id))",
    "CREATE INDEX IF NOT EXISTS slots_start ON slots (slot_start_utc)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '" + SCHEMA_VERSION + "')",
)
SELECT_SQL: Final[str] = (
    "SELECT slot_id, youtube_channel_id, slot_start_utc, stage, updated_at, record_json"
    " FROM slots WHERE slot_id = ? AND youtube_channel_id = ?"
)
UPSERT_SQL: Final[str] = (
    "INSERT INTO slots (slot_id, youtube_channel_id, slot_start_utc, stage, updated_at, record_json)"
    " VALUES (?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (slot_id, youtube_channel_id) DO UPDATE SET"
    " slot_start_utc = excluded.slot_start_utc, stage = excluded.stage,"
    " updated_at = excluded.updated_at, record_json = excluded.record_json"
)
DELETE_SQL: Final[str] = "DELETE FROM slots WHERE slot_start_utc < ?"
HAS_SLOTS_SQL: Final[str] = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'slots'"
META_CREATED_UTC: Final[str] = "created_utc"
SELECT_META_SQL: Final[str] = "SELECT value FROM meta WHERE key = ?"
INSERT_META_SQL: Final[str] = "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)"


class RecordStore:
    """Одна база на запуск. path — файл базы (None — только в памяти, тесты); is_new — файла не было."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path | None,
        *,
        is_new: bool,
        read_only: bool,
        created_utc: datetime,
        warnings: list[str] | None = None,
    ) -> None:
        self._connection: sqlite3.Connection = connection
        self._path: Path | None = path
        self._is_new: bool = is_new
        self._created_utc: datetime = created_utc
        self._read_only: bool = read_only
        self._warnings: list[str] = list(warnings or [])
        self._is_write_failure_reported: bool = False
        self._is_closed: bool = False

    @classmethod
    def memory(
        cls,
        *,
        is_new: bool = True,
        path: Path | None = None,
        read_only: bool = False,
        created_utc: datetime | None = None,
    ) -> RecordStore:
        """Пустая база в памяти: для тестов и для read_only без файла. created_utc по умолчанию — сейчас."""
        connection: sqlite3.Connection = sqlite3.connect(MEMORY_DATABASE)
        _create_schema(connection)
        moment: datetime = created_utc if created_utc is not None else datetime.now(timezone.utc)
        return cls(connection, path, is_new=is_new, read_only=read_only, created_utc=moment)

    @classmethod
    def open(cls, path: Path, *, read_only: bool, now_local: datetime) -> RecordStore:
        now_utc: datetime = now_local.astimezone(timezone.utc)
        if read_only:
            return cls._open_read_only(path, now_utc)
        is_new: bool = not path.exists()
        try:
            connection, created_utc = _connect_writable(path, now_utc, is_new=is_new)
            return cls(connection, path, is_new=is_new, read_only=False, created_utc=created_utc)
        except sqlite3.DatabaseError as error:
            renamed: Path = path.with_name(path.name + BROKEN_SUFFIX.format(stamp=now_local.strftime(FILE_STAMP_FORMAT)))
            LOGGER.warning("records_broken path=%s renamed=%s error=%s", path, renamed.name, error)
            try:
                path.rename(renamed)
            except OSError as rename_error:
                # файл занят или защищён: запуск идёт без записи, как в read_only
                LOGGER.warning("records_rename_failed path=%s reason=%s", path, rename_error)
                store: RecordStore = cls.memory(is_new=True, path=path, read_only=True, created_utc=now_utc)
                store._warnings.append(msg.WARNING_RECORDS_BROKEN_READ_ONLY.format(path=path))
                return store
            warning: str = msg.WARNING_RECORDS_BROKEN.format(path=path, renamed=renamed.name)
            connection, created_utc = _connect_writable(path, now_utc, is_new=True)
            return cls(connection, path, is_new=True, read_only=False, created_utc=created_utc, warnings=[warning])

    @classmethod
    def _open_read_only(cls, path: Path, now_utc: datetime) -> RecordStore:
        """Ничего не пишет на диск: файла нет или в нём нет таблицы — пустая база в памяти; нет created_utc — сейчас."""
        if not path.exists():
            return cls.memory(is_new=True, path=path, read_only=True, created_utc=now_utc)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(READ_ONLY_URI.format(path=path.as_posix()), uri=True)
            if connection.execute(HAS_SLOTS_SQL).fetchone() is None:
                connection.close()
                return cls.memory(is_new=True, path=path, read_only=True, created_utc=now_utc)
            created_utc: datetime = _read_created_utc(connection) or now_utc
            return cls(connection, path, is_new=False, read_only=True, created_utc=created_utc)
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            LOGGER.warning("records_broken path=%s renamed=- error=%s", path, error)
            store: RecordStore = cls.memory(is_new=True, path=path, read_only=True, created_utc=now_utc)
            store._warnings.append(msg.WARNING_RECORDS_BROKEN_READ_ONLY.format(path=path))
            return store

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def is_new(self) -> bool:
        return self._is_new

    @property
    def created_utc(self) -> datetime:
        """Когда появилась память (aware UTC)."""
        return self._created_utc

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    def take_warnings(self) -> list[str]:
        taken: list[str] = list(self._warnings)
        self._warnings.clear()
        return taken

    def find(self, slot_id: str, youtube_channel_id: str) -> SlotRecord | None:
        try:
            row: tuple[str, ...] | None = self._connection.execute(SELECT_SQL, (slot_id, youtube_channel_id)).fetchone()
        except sqlite3.Error as error:
            LOGGER.warning(
                "records_read_failed slot_id=%s youtube_channel_id=%s reason=%s", slot_id, youtube_channel_id, error
            )
            return None
        return SlotRecord.from_row(*row) if row is not None else None

    def save(self, record: SlotRecord, requested: SlotStage | None = None) -> bool:
        """Upsert одной транзакцией; read_only — ничего не пишет. Сбой — False и строка предупреждения один раз.

        requested — стадия, которую просил код; в лог идёт, если легла другая (стадия не откатывается).
        """
        if self._read_only:
            return False
        values: tuple[str, ...] = (
            record.slot_id,
            record.youtube_channel_id,
            record.slot_start_utc,
            record.stage.value,
            record.updated_at,
            record.record_json(),
        )
        try:
            with self._connection:
                self._connection.execute(UPSERT_SQL, values)
        except (sqlite3.Error, OSError) as error:
            self._report_write_failure(record, error)
            return False
        requested_part: str = (
            f" requested={requested.value}" if requested is not None and requested is not record.stage else ""
        )
        LOGGER.info(
            "record_saved slot_id=%s youtube_channel_id=%s stage=%s%s",
            record.slot_id,
            record.youtube_channel_id,
            record.stage.value,
            requested_part,
        )
        return True

    def delete_started_before(self, border_utc: datetime) -> int:
        """Записи слотов, начавшихся раньше границы; read_only и сбой — 0."""
        if self._read_only:
            return 0
        try:
            with self._connection:
                removed: int = self._connection.execute(DELETE_SQL, (border_utc.isoformat(),)).rowcount
        except (sqlite3.Error, OSError) as error:
            LOGGER.warning("records_clean_failed reason=%s", error)
            return 0
        return max(removed, 0)

    def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        self._connection.close()

    @property
    def is_closed(self) -> bool:
        return self._is_closed

    def _report_write_failure(self, record: SlotRecord, error: Exception) -> None:
        LOGGER.warning(
            "records_write_failed slot_id=%s youtube_channel_id=%s reason=%s",
            record.slot_id,
            record.youtube_channel_id,
            error,
        )
        if self._is_write_failure_reported:
            return
        self._is_write_failure_reported = True
        self._warnings.append(msg.WARNING_RECORDS_WRITE_FAILED.format(path=self._path or MEMORY_DATABASE, error=error))


def _connect_writable(path: Path, now_utc: datetime, *, is_new: bool) -> tuple[sqlite3.Connection, datetime]:
    """Открыть и проверить: не база — sqlite3.DatabaseError (соединение закрыто, файл можно переименовать).

    Нет created_utc — записать now_utc (у существующей базы — строкой лога).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        created_utc: datetime | None = _read_created_utc(connection)
        if created_utc is None:
            created_utc = now_utc
            with connection:
                connection.execute(INSERT_META_SQL, (META_CREATED_UTC, now_utc.isoformat()))
            if not is_new:
                LOGGER.info("records_created_utc_initialized value=%s", now_utc.isoformat())
    except sqlite3.DatabaseError:
        connection.close()
        raise
    return connection, created_utc


def _read_created_utc(connection: sqlite3.Connection) -> datetime | None:
    """created_utc из meta; нет строки или она не разбирается — None."""
    row: tuple[str] | None = connection.execute(SELECT_META_SQL, (META_CREATED_UTC,)).fetchone()
    if row is None:
        return None
    try:
        parsed: datetime = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _create_schema(connection: sqlite3.Connection) -> None:
    with connection:
        for statement in SCHEMA_SQL:
            connection.execute(statement)
