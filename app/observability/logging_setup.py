"""Логи планера: файл logs\\{дата}_{время}_planer.log (DEBUG); маскирование ключей потока.

По образцу broadcaster app/bootstrap/logging_config.py, урезанный. Ключ потока
попадает в лог только через mask_stream_key (CLAUDE.md, инвариант 6).

Терминал — только тексты для владельца (messages_ru, печатает main.py): сырой лог идёт в терминал
только с --debug. Сторонние библиотеки (корневой логгер Python: googleapiclient, google_auth_oauthlib,
urllib3…) и предупреждения warnings пишутся от WARNING в тот же файл; без --debug в терминал не попадают:
у корневого логгера есть свой обработчик, поэтому logging.lastResort не срабатывает.
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
THIRD_PARTY_LEVEL: Final[int] = logging.WARNING   # сторонние логгеры — в файл от этого уровня

_THIRD_PARTY_HANDLERS: list[logging.Handler] = []   # свои обработчики на корневом логгере Python


def get_logger(name: str) -> logging.Logger:
    """Дочерний логгер планера: planer.<name>."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def setup_logging(logs_dir: Path, debug: bool) -> Path:
    """Файл — всегда DEBUG (сторонние — от WARNING); терминал — только с --debug (DEBUG в stderr)."""
    close_logging()
    formatter: logging.Formatter = logging.Formatter(LOG_FORMAT)
    stamp: str = datetime.now().astimezone().strftime(FILE_STAMP_FORMAT)
    log_path: Path = logs_dir / LOG_FILE_TEMPLATE.format(stamp=stamp)
    file_handler: logging.FileHandler = logging.FileHandler(log_path, encoding=LOG_ENCODING)
    handlers: list[logging.Handler] = [file_handler]
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))
    planer_logger: logging.Logger = logging.getLogger(ROOT_LOGGER_NAME)
    planer_logger.setLevel(logging.DEBUG)
    planer_logger.propagate = False
    for handler in handlers:
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(formatter)
        handler.addFilter(_ThirdPartyFilter())
        planer_logger.addHandler(handler)
    _attach_third_party(handlers)
    return log_path


def close_logging() -> None:
    """Снять и закрыть обработчики (на Windows открытый файл лога не даёт удалить папку).

    С корневого логгера Python снимаются только свои обработчики: чужие (pytest caplog) остаются.
    """
    python_root: logging.Logger = logging.getLogger()
    for handler in _THIRD_PARTY_HANDLERS:
        python_root.removeHandler(handler)
    _THIRD_PARTY_HANDLERS.clear()
    planer_logger: logging.Logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(planer_logger.handlers):
        planer_logger.removeHandler(handler)
        handler.close()
    logging.captureWarnings(False)


def mask_stream_key(value: str | None) -> str:
    """None → "-"; короче 4 символов → "****"; иначе "****-" + последние 4 символа."""
    if value is None:
        return MASK_EMPTY
    if len(value) < MASK_VISIBLE_CHARS:
        return MASK_HIDDEN
    return MASK_PREFIX + value[-MASK_VISIBLE_CHARS:]


class _ThirdPartyFilter(logging.Filter):
    """Записи планера проходят с любым уровнем, чужие — от THIRD_PARTY_LEVEL."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == ROOT_LOGGER_NAME or record.name.startswith(ROOT_LOGGER_NAME + "."):
            return True
        return record.levelno >= THIRD_PARTY_LEVEL


def _attach_third_party(handlers: list[logging.Handler]) -> None:
    """Корневой логгер Python получает те же обработчики; warnings.warn — через логгер py.warnings."""
    python_root: logging.Logger = logging.getLogger()
    for handler in handlers:
        python_root.addHandler(handler)
        _THIRD_PARTY_HANDLERS.append(handler)
    logging.captureWarnings(True)
