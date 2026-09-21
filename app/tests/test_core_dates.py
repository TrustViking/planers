from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.dates import (
    DATE_FORMAT,
    DATETIME_FORMAT,
    FILE_STAMP_FORMAT,
    SLOT_TIME_FORMAT,
    TIME_FORMAT,
    build_slot_id,
    format_date,
    format_datetime_text,
    format_now_local,
    format_time,
    parse_date,
    parse_datetime_text,
    parse_local_datetime_text_utc,
    parse_iso_start,
    parse_time,
    youtube_quota_day,
    youtube_quota_day_start,
)


def test_formats_are_the_documented_ones() -> None:
    assert DATE_FORMAT == "%d-%m-%Y"
    assert TIME_FORMAT == "%H:%M"
    assert DATETIME_FORMAT == "%d-%m-%Y %H:%M"
    assert FILE_STAMP_FORMAT == "%d-%m-%Y_%H%M%S"
    assert SLOT_TIME_FORMAT == "%H%M"


def test_date_time_and_datetime_round_trip() -> None:
    assert format_date(parse_date("17-03-2027")) == "17-03-2027"
    assert format_time(parse_time("19:00")) == "19:00"
    value: datetime = parse_datetime_text("13-09-2026 10:15")
    assert value == datetime(2026, 9, 13, 10, 15)
    assert format_datetime_text(value) == "13-09-2026 10:15"


def test_build_slot_id_follows_manifest_contract() -> None:
    assert build_slot_id("17-03-2027", "19:00", "uk") == "17-03-2027_1900_uk"


def test_build_slot_id_rejects_iso_date() -> None:
    with pytest.raises(ValueError):
        build_slot_id("2027-03-17", "19:00", "uk")


def test_parse_iso_start_keeps_offset() -> None:
    value: datetime = parse_iso_start("2027-03-17T19:00:00+02:00")
    assert value.utcoffset() == timedelta(hours=2)
    assert value.astimezone(timezone.utc) == datetime(2027, 3, 17, 17, 0, tzinfo=timezone.utc)


def test_parse_iso_start_rejects_naive() -> None:
    with pytest.raises(ValueError):
        parse_iso_start("2027-03-17T19:00:00")


def test_format_now_local_is_parseable() -> None:
    assert isinstance(parse_datetime_text(format_now_local()), datetime)


def test_local_text_becomes_the_same_moment_in_utc() -> None:
    """Моменты памяти планера — местное DD-MM-YYYY HH:MM; created_utc — ISO-8601 UTC (5n-B)."""
    moment: datetime = datetime(2026, 9, 17, 16, 39).astimezone()      # местное время машины
    parsed: datetime = parse_local_datetime_text_utc(format_datetime_text(moment))
    assert parsed.tzinfo is timezone.utc and parsed == moment


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("moment", "day", "start"),
    [
        ("2026-09-21T06:59", "20-09-2026", "2026-09-20T07:00"),   # лето: граница 07:00 UTC
        ("2026-09-21T07:00", "21-09-2026", "2026-09-21T07:00"),
        ("2026-12-10T07:59", "09-12-2026", "2026-12-09T08:00"),   # зима: граница 08:00 UTC
        ("2026-12-10T08:00", "10-12-2026", "2026-12-10T08:00"),
        ("2026-03-08T07:59", "07-03-2026", "2026-03-07T08:00"),   # 08-03-2026 — второе воскресенье марта
        ("2026-03-08T08:00", "08-03-2026", "2026-03-08T08:00"),   # полночь дня перехода — ещё PST
        ("2026-03-09T07:00", "09-03-2026", "2026-03-09T07:00"),   # на следующий день — уже PDT
        ("2026-11-01T06:59", "31-10-2026", "2026-10-31T07:00"),   # 01-11-2026 — первое воскресенье ноября
        ("2026-11-01T07:00", "01-11-2026", "2026-11-01T07:00"),   # полночь дня перехода — ещё PDT
        ("2026-11-02T07:59", "01-11-2026", "2026-11-01T07:00"),
        ("2026-11-02T08:00", "02-11-2026", "2026-11-02T08:00"),   # на следующий день — уже PST
    ],
)
def test_youtube_quota_day_starts_at_pacific_midnight(moment: str, day: str, start: str) -> None:
    assert format_date(youtube_quota_day(_utc(moment))) == day
    assert youtube_quota_day_start(_utc(moment)) == _utc(start)
