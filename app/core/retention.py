"""Чистка старья (ТЗ §5.7): файлы старше `keep_days` дней в `promo\\` и `logs\\`.

Единственное место, где планер что-то удаляет. Срок один на обе папки и берётся
из конфига; текущий лог и сегодняшний отчёт под него не попадают по возрасту.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.observability.logging_setup import get_logger
from app.paths import PlanerPaths

LOGGER = get_logger("retention")


def cleanup_expired(paths: PlanerPaths, keep_days: int, now: datetime) -> list[Path]:
    """Удаляет файлы, изменённые раньше, чем keep_days дней назад; возвращает удалённые."""
    border: datetime = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for directory in (paths.promo_dir, paths.logs_dir):
        for file_path in _files_older_than(directory, border):
            try:
                file_path.unlink()
            except OSError as error:
                LOGGER.warning("cleanup_failed file=%s reason=%s", file_path, error)
                continue
            LOGGER.info("cleanup_removed file=%s", file_path)
            removed.append(file_path)
    return removed


def _files_older_than(directory: Path, border: datetime) -> Iterable[Path]:
    """Только файлы самой папки: вложенное планер не трогает."""
    if not directory.is_dir():
        return []
    return [
        file_path
        for file_path in sorted(directory.iterdir())
        if file_path.is_file()
        and datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc) < border
    ]
