"""Строка статистики запуска в памяти планера: таблица runs файла secrets\\planer.sqlite3.

Одна строка на запуск (RunRecord): run_id — отметка из имени файла лога этого запуска, колонки поиска
(started_utc, quota_day, quota_units, elapsed_sec, exit_code, mode, version) и всё остальное из RunStats —
одним JSON (stats_json). Это замер, а не память о слотах: строка пишется в любом режиме, в том числе
в --dry-run и --status (явное исключение из «read_only — на диск ничего не пишется»), и по keep_days
не чистится — история для анализа. Пишет её RunStore отдельным коротким соединением в конце запуска;
таблицу slots он не трогает. Сбой записи — WARNING run_stats_write_failed, запуск идёт дальше.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.observability.logging_setup import get_logger
from app.records.record_store import SCHEMA_VERSION, SCHEMA_VERSION_SQL

LOGGER = get_logger("records")

RUNS_SCHEMA_SQL: Final[tuple[str, ...]] = (
    "CREATE TABLE IF NOT EXISTS runs ("
    " run_id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, quota_day TEXT NOT NULL,"
    " quota_units INTEGER NOT NULL, elapsed_sec REAL NOT NULL, exit_code INTEGER NOT NULL,"
    " mode TEXT NOT NULL, version TEXT NOT NULL, stats_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS runs_quota_day ON runs (quota_day)",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)
INSERT_RUN_SQL: Final[str] = (
    "INSERT OR REPLACE INTO runs"
    " (run_id, started_utc, quota_day, quota_units, elapsed_sec, exit_code, mode, version, stats_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
SUM_QUOTA_DAY_SQL: Final[str] = "SELECT COALESCE(SUM(quota_units), 0) FROM runs WHERE quota_day = ?"
STATS_JSON_OPTIONS: Final[dict[str, Any]] = {"ensure_ascii": False, "sort_keys": False}


@dataclass(frozen=True)
class RunRecord:
    """Строка таблицы runs."""

    run_id: str          # отметка из имени файла лога: DD-MM-YYYY_HHMMSS
    started_utc: str     # ISO-8601 UTC — для сортировки
    quota_day: str       # квотные сутки YouTube, DD-MM-YYYY
    quota_units: int     # ≈ единиц квоты за этот запуск (оценка сверху)
    elapsed_sec: float
    exit_code: int
    mode: str
    version: str
    stats: dict[str, Any]

    def values(self) -> tuple[Any, ...]:
        return (
            self.run_id,
            self.started_utc,
            self.quota_day,
            self.quota_units,
            self.elapsed_sec,
            self.exit_code,
            self.mode,
            self.version,
            json.dumps(self.stats, **STATS_JSON_OPTIONS),
        )


class RunStore:
    """Запись строки runs и сумма квоты за квотные сутки — одним коротким соединением."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def save(self, record: RunRecord) -> int | None:
        """Записать строку этого запуска и вернуть ≈ единиц за её квотные сутки по всем строкам этого компьютера.

        Сбой — WARNING run_stats_write_failed и None («нет данных»); исключение наружу не идёт.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection: sqlite3.Connection = sqlite3.connect(self._path)
            try:
                with connection:
                    for statement in RUNS_SCHEMA_SQL:
                        connection.execute(statement)
                    connection.execute(SCHEMA_VERSION_SQL, (SCHEMA_VERSION,))
                    connection.execute(INSERT_RUN_SQL, record.values())
                row: tuple[int] = connection.execute(SUM_QUOTA_DAY_SQL, (record.quota_day,)).fetchone()
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as error:
            LOGGER.warning("run_stats_write_failed path=%s run_id=%s reason=%s", self._path, record.run_id, error)
            return None
        LOGGER.info("run_stats_saved run_id=%s quota_day=%s quota_units=%d", record.run_id, record.quota_day, record.quota_units)
        return int(row[0])
