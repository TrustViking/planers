"""Файл ключей keystreams\\keys.txt (ТЗ §5.5): производный, перегенерируется при каждом запуске.

Проверка формата ключа (§7.4, пометка «формат?») — этап 3.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from app.core.dates import format_datetime_text, parse_date
from app.paths import PlanerPaths
from app.platforms.base import StreamInfo
from app.state.registry import FormStatus, Registration
from app.ui import messages_ru as msg

FIELD_SEPARATOR: Final[str] = " | "
KEYS_ENCODING: Final[str] = "utf-8"
MISSING_VALUE: Final[str] = "-"


@dataclass(frozen=True)
class KeyRow:
    language: str
    date: str
    time: str
    account_name: str
    form_status_text: str
    stream_url: str
    stream_key: str
    broadcast_url: str


def form_status_text(registration: Registration | None) -> str:
    """None — эфир есть на площадке, в журнале его нет (только --status)."""
    if registration is None:
        return msg.KEY_FORM_NEVER_SENT
    if registration.form_status is FormStatus.SENT:
        sent_at: str = (
            format_datetime_text(registration.form_sent_at) if registration.form_sent_at else MISSING_VALUE
        )
        return msg.KEY_FORM_SENT.format(sent_at=sent_at)
    if registration.last_error:
        return msg.KEY_FORM_FAILED.format(error=registration.last_error)
    return msg.KEY_FORM_WAITING


def key_row_from_registration(registration: Registration) -> KeyRow:
    return KeyRow(
        language=registration.language,
        date=registration.date,
        time=registration.time,
        account_name=registration.account_name,
        form_status_text=form_status_text(registration),
        stream_url=registration.stream_url or MISSING_VALUE,
        stream_key=registration.stream_key or MISSING_VALUE,
        broadcast_url=registration.broadcast_url or MISSING_VALUE,
    )


def key_row_from_platform(
    *,
    language: str,
    date_text: str,
    time_text: str,
    account_name: str,
    stream: StreamInfo,
    broadcast_url: str,
    registration: Registration | None,
) -> KeyRow:
    """Эфир на площадке; ключ — с площадки, статус формы — из журнала, если регистрация есть."""
    return KeyRow(
        language=language,
        date=date_text,
        time=time_text,
        account_name=account_name,
        form_status_text=form_status_text(registration),
        stream_url=stream.ingestion_address,
        stream_key=stream.stream_name,
        broadcast_url=broadcast_url,
    )


def _row_order(row: KeyRow) -> tuple[date, str, str]:
    return (parse_date(row.date), row.time, row.language)


def render_keys_file(rows: Iterable[KeyRow], generated_at_text: str) -> str:
    lines: list[str] = [msg.KEYS_FILE_HEADER.format(generated_at=generated_at_text), msg.KEYS_FILE_COLUMNS]
    for row in sorted(rows, key=_row_order):
        lines.append(
            FIELD_SEPARATOR.join(
                (
                    row.language,
                    row.date,
                    row.time,
                    row.account_name,
                    row.form_status_text,
                    row.stream_url,
                    row.stream_key,
                    row.broadcast_url,
                )
            )
        )
    return "\n".join(lines) + "\n"


def write_keys_file(paths: PlanerPaths, text: str) -> Path:
    paths.keystreams_dir.mkdir(parents=True, exist_ok=True)
    paths.keys_file.write_text(text, encoding=KEYS_ENCODING)
    return paths.keys_file
