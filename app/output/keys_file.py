"""Файл ключей keystreams\\keys.txt (ТЗ §5.5): производный, перегенерируется при каждом запуске.

Строки строятся из объекта запланированного эфира, а в --status — из того, что нашлось
на площадке: там объектов нет, есть эфиры с маркером планера. Журнал не читается:
статус формы говорит только о том, что планер сделал в этом запуске (ТЗ §5.5).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final

from app.core.dates import format_datetime_text, parse_date
from app.output.report import form_reason_text
from app.paths import PlanerPaths
from app.pipeline.plan import PlannedBroadcast
from app.pipeline.reconciler import MarkedBroadcast
from app.platforms.base import broadcast_url_for
from app.ui import messages_ru as msg

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


def form_status_text(item: PlannedBroadcast) -> str:
    """Только этот запуск: передан сейчас, должен был уйти и не ушёл, или в этом запуске не отправлялся."""
    if item.is_form_sent:
        return msg.KEY_FORM_SENT.format(
            sent_at=format_datetime_text(item.form_sent_at) if item.form_sent_at else MISSING_VALUE
        )
    if item.should_send_key:
        return msg.KEY_FORM_FAILED.format(reason=form_reason_text(item.last_error))
    return msg.KEY_FORM_KEPT


def key_row_from_planned(item: PlannedBroadcast) -> KeyRow:
    """Строка по объекту: всё, что нужно, у него уже есть."""
    return KeyRow(
        language=item.language,
        date=item.date,
        time=item.time,
        account_name=item.account_name,
        form_status_text=form_status_text(item),
        stream_url=item.stream_url or MISSING_VALUE,
        stream_key=item.stream_key or MISSING_VALUE,
        broadcast_url=item.broadcast_url or MISSING_VALUE,
    )


def key_row_from_marked(marked: MarkedBroadcast) -> KeyRow:
    """--status: ключ с площадки; в форму этот режим ничего не передаёт."""
    return KeyRow(
        language=marked.parts.language,
        date=marked.parts.date,
        time=marked.parts.time,
        account_name=marked.channel.account_name,
        form_status_text=msg.KEY_FORM_KEPT,
        stream_url=marked.stream.ingestion_address,
        stream_key=marked.stream.stream_name,
        broadcast_url=broadcast_url_for(marked.channel, marked.broadcast.broadcast_id),
    )


def _row_order(row: KeyRow) -> tuple[date, str, str]:
    return (parse_date(row.date), row.time, row.language)


def render_keys_file(rows: Iterable[KeyRow], generated_at_text: str) -> str:
    """Блок на стрим, между блоками пустая строка: ключ не уезжает за край экрана (ТЗ §5.5)."""
    lines: list[str] = [line.format(generated_at=generated_at_text) for line in msg.KEYS_FILE_HEADER]
    for row in sorted(rows, key=_row_order):
        lines.append("")
        lines.extend(_row_block(row))
    return "\n".join(lines) + "\n"


def _row_block(row: KeyRow) -> list[str]:
    return [
        msg.KEYS_BLOCK_TITLE.format(date=row.date, time=row.time, language=row.language, account_name=row.account_name),
        msg.KEYS_BLOCK_KEY.format(value=row.stream_key),
        msg.KEYS_BLOCK_STREAM.format(value=row.stream_url),
        msg.KEYS_BLOCK_BROADCAST.format(value=row.broadcast_url),
        msg.KEYS_BLOCK_FORM.format(value=row.form_status_text),
    ]


def write_keys_file(paths: PlanerPaths, text: str) -> Path:
    paths.keystreams_dir.mkdir(parents=True, exist_ok=True)
    paths.keys_file.write_text(text, encoding=KEYS_ENCODING)
    return paths.keys_file
