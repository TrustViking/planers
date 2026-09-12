"""Форматы дат и времени планера — единственный источник (ТЗ §5)."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Final

DATE_FORMAT: Final[str] = "%d-%m-%Y"
TIME_FORMAT: Final[str] = "%H:%M"
DATETIME_FORMAT: Final[str] = f"{DATE_FORMAT} {TIME_FORMAT}"
FILE_STAMP_FORMAT: Final[str] = "%d-%m-%Y_%H%M%S"   # имя файла: дата_время_наименование
SLOT_TIME_FORMAT: Final[str] = "%H%M"
SLOT_ID_TEMPLATE: Final[str] = "{date}_{time}_{language}"


def parse_date(text: str) -> date:
    return datetime.strptime(text, DATE_FORMAT).date()


def format_date(value: date) -> str:
    return value.strftime(DATE_FORMAT)


def parse_time(text: str) -> time:
    return datetime.strptime(text, TIME_FORMAT).time()


def format_time(value: time) -> str:
    return value.strftime(TIME_FORMAT)


def parse_datetime_text(text: str) -> datetime:
    """DD-MM-YYYY HH:MM → naive datetime (местное время; только для сортировки и отчёта)."""
    return datetime.strptime(text, DATETIME_FORMAT)


def format_datetime_text(value: datetime) -> str:
    return value.strftime(DATETIME_FORMAT)


def parse_iso_start(text: str) -> datetime:
    """`start` манифеста: ISO-8601 со смещением; без смещения — ValueError."""
    value: datetime = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"start without UTC offset: {text!r}")
    return value


def format_now_local() -> str:
    """Сейчас по часам машины, в DATETIME_FORMAT."""
    return format_datetime_text(datetime.now().astimezone())


def build_slot_id(date_text: str, time_text: str, language: str) -> str:
    """slot_id = {DD-MM-YYYY}_{HHMM}_{lang} (ТЗ §5.1); неверные дата или время — ValueError."""
    return SLOT_ID_TEMPLATE.format(
        date=format_date(parse_date(date_text)),
        time=parse_time(time_text).strftime(SLOT_TIME_FORMAT),
        language=language,
    )
