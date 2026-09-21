"""Форматы дат и времени планера — единственный источник (ТЗ §5)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
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


def parse_local_datetime_text_utc(text: str) -> datetime:
    """DD-MM-YYYY HH:MM местного времени машины (так пишутся моменты в памяти планера) → момент в UTC."""
    return parse_datetime_text(text).astimezone(timezone.utc)


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


# --- квотные сутки YouTube Data API: начинаются в полночь по Тихоокеанскому времени (правило США, stdlib без tzdata)
DATETIME_SECONDS_FORMAT: Final[str] = f"{DATE_FORMAT} %H:%M:%S"   # моменты внутри статистики запуска
PACIFIC_STANDARD_OFFSET_HOURS: Final[int] = -8   # PST
PACIFIC_DAYLIGHT_OFFSET_HOURS: Final[int] = -7   # PDT
DST_START_MONTH: Final[int] = 3                  # второе воскресенье марта, 02:00 местного (PST)
DST_START_SUNDAY_NUMBER: Final[int] = 2
DST_END_MONTH: Final[int] = 11                   # первое воскресенье ноября, 02:00 местного (PDT)
DST_END_SUNDAY_NUMBER: Final[int] = 1
DST_SWITCH_LOCAL_HOUR: Final[int] = 2
SUNDAY_WEEKDAY: Final[int] = 6


def format_datetime_seconds(value: datetime) -> str:
    return value.strftime(DATETIME_SECONDS_FORMAT)


def _nth_sunday(year: int, month: int, number: int) -> date:
    first: date = date(year, month, 1)
    shift: int = (SUNDAY_WEEKDAY - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (number - 1))


def _pacific_offset_hours(moment: datetime) -> int:
    """Смещение Тихоокеанского времени от UTC в момент moment (aware): −7 летом, −8 зимой."""
    moment_utc: datetime = moment.astimezone(timezone.utc)
    year: int = moment_utc.year
    start_local: date = _nth_sunday(year, DST_START_MONTH, DST_START_SUNDAY_NUMBER)
    end_local: date = _nth_sunday(year, DST_END_MONTH, DST_END_SUNDAY_NUMBER)
    dst_start_utc: datetime = datetime.combine(start_local, time(DST_SWITCH_LOCAL_HOUR), timezone.utc) - timedelta(
        hours=PACIFIC_STANDARD_OFFSET_HOURS
    )
    dst_end_utc: datetime = datetime.combine(end_local, time(DST_SWITCH_LOCAL_HOUR), timezone.utc) - timedelta(
        hours=PACIFIC_DAYLIGHT_OFFSET_HOURS
    )
    if dst_start_utc <= moment_utc < dst_end_utc:
        return PACIFIC_DAYLIGHT_OFFSET_HOURS
    return PACIFIC_STANDARD_OFFSET_HOURS


def youtube_quota_day(moment: datetime) -> date:
    """Квотные сутки YouTube, в которые попадает момент (aware): дата по Тихоокеанскому времени."""
    moment_utc: datetime = moment.astimezone(timezone.utc)
    return (moment_utc + timedelta(hours=_pacific_offset_hours(moment_utc))).date()


def youtube_quota_day_start(moment: datetime) -> datetime:
    """Начало квотных суток момента — полночь по Тихоокеанскому времени, aware UTC.

    Переход на летнее и зимнее время — в 02:00 местного, полночь дня перехода живёт по прежнему смещению.
    """
    day: date = youtube_quota_day(moment)
    midnight: datetime = datetime.combine(day, time(0), timezone.utc)
    for offset in (PACIFIC_DAYLIGHT_OFFSET_HOURS, PACIFIC_STANDARD_OFFSET_HOURS):
        candidate: datetime = midnight - timedelta(hours=offset)
        if _pacific_offset_hours(candidate) == offset:
            return candidate
    return midnight - timedelta(hours=PACIFIC_STANDARD_OFFSET_HOURS)
