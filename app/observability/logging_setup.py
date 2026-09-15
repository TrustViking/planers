"""Логи планера: файл logs\\{дата}_{время}_planer.log (DEBUG) + консоль; маскирование ключей потока.

По образцу broadcaster app/bootstrap/logging_config.py, урезанный. Ключ потока
попадает в лог только через mask_stream_key (CLAUDE.md, инвариант 6).
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Final

from app.core.dates import FILE_STAMP_FORMAT

ROOT_LOGGER_NAME: Final[str] = "planer"
LOG_FORMAT: Final[str] = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_FILE_TEMPLATE: Final[str] = "{stamp}_planer.log"
LOG_ENCODING: Final[str] = "utf-8"
MASK_PREFIX: Final[str] = "****-"
MASK_HIDDEN: Final[str] = "****"
MASK_EMPTY: Final[str] = "-"
MASK_VISIBLE_CHARS: Final[int] = 4


def get_logger(name: str) -> logging.Logger:
    """Дочерний логгер планера: planer.<name>."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def setup_logging(logs_dir: Path, debug: bool) -> Path:
    """Файл — всегда DEBUG; консоль — WARNING (DEBUG при --debug): владелец видит отчёт и предупреждения."""
    close_logging()
    root_logger: logging.Logger = logging.getLogger(ROOT_LOGGER_NAME)
    root_logger.setLevel(logging.DEBUG)
    root_logger.propagate = False
    formatter: logging.Formatter = logging.Formatter(LOG_FORMAT)
    stamp: str = datetime.now().astimezone().strftime(FILE_STAMP_FORMAT)
    log_path: Path = logs_dir / LOG_FILE_TEMPLATE.format(stamp=stamp)
    file_handler: logging.FileHandler = logging.FileHandler(log_path, encoding=LOG_ENCODING)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    console_handler: logging.StreamHandler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    return log_path


def close_logging() -> None:
    """Снять и закрыть обработчики (на Windows открытый файл лога не даёт удалить папку)."""
    root_logger: logging.Logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()


def mask_stream_key(value: str | None) -> str:
    """None → "-"; короче 4 символов → "****"; иначе "****-" + последние 4 символа."""
    if value is None:
        return MASK_EMPTY
    if len(value) < MASK_VISIBLE_CHARS:
        return MASK_HIDDEN
    return MASK_PREFIX + value[-MASK_VISIBLE_CHARS:]
