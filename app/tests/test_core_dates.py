from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.dates import (
    DATE_FORMAT,
    DATETIME_FORMAT,
    REPORT_STAMP_FORMAT,
    SLOT_TIME_FORMAT,
    TIME_FORMAT,
    build_slot_id,
    format_date,
    format_datetime_text,
    format_now_local,
    format_time,
    parse_date,
    parse_datetime_text,
    parse_iso_start,
    parse_time,
)


def test_formats_are_the_documented_ones() -> None:
    assert DATE_FORMAT == "%d-%m-%Y"
    assert TIME_FORMAT == "%H:%M"
    assert DATETIME_FORMAT == "%d-%m-%Y %H:%M"
    assert REPORT_STAMP_FORMAT == "%d-%m-%Y_%H%M%S"
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
