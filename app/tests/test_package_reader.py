from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.package.model import Package, read_preview
from app.package.reader import PackageError, PackageErrorReason, read_package


def _reason_of(path: Path) -> PackageError:
    with pytest.raises(PackageError) as raised:
        read_package(path)
    return raised.value


def test_valid_package_is_read(make_package: Callable[..., Path], make_slot: Callable[..., dict[str, Any]]) -> None:
    path: Path = make_package(
        slots=[make_slot("17-03-2027", "19:00", "uk", previews=2), make_slot("18-03-2027", "20:00", "en")]
    )
    package: Package = read_package(path)
    assert package.path == path
    assert package.generated_at == datetime(2026, 9, 13, 10, 15)
    assert (package.period_from, package.period_to) == ("17-03-2027", "18-03-2027")
    assert package.form.fields["slot_id"] is None
    assert package.form.values["platform"]["youtube"] == "You Tube"
    assert package.form.date_format == "%d.%m.%Y"
    assert [slot.slot_id for slot in package.slots] == ["17-03-2027_1900_uk", "18-03-2027_2000_en"]
    assert package.slots[0].start.utcoffset() == timedelta(hours=2)
    assert package.slots[0].previews == ("previews/17-03-2027_1900_uk_1.jpg", "previews/17-03-2027_1900_uk_2.jpg")


def test_not_zip(tmp_path: Path) -> None:
    path: Path = tmp_path / "broken.bcast"
    path.write_bytes(b"not a zip at all")
    assert _reason_of(path).reason is PackageErrorReason.NOT_ZIP


def test_no_manifest(make_package: Callable[..., Path]) -> None:
    assert _reason_of(make_package(include_manifest=False)).reason is PackageErrorReason.NO_MANIFEST


def test_unsupported_schema_version(make_package: Callable[..., Path]) -> None:
    error: PackageError = _reason_of(make_package(schema_version=2))
    assert error.reason is PackageErrorReason.UNSUPPORTED_SCHEMA
    assert error.detail == "2"


def test_missing_form_field_slot_id(make_package: Callable[..., Path]) -> None:
    error: PackageError = _reason_of(make_package(manifest_edit=lambda m: m["form"]["fields"].pop("slot_id")))
    assert error.reason is PackageErrorReason.MISSING_KEY
    assert error.detail == "form.fields.slot_id"


@pytest.mark.parametrize(
    ("key", "value"),
    [("date", "18-03-2027"), ("time", "20:00"), ("language", "ru")],
)
def test_slot_id_must_match_date_time_language(make_package: Callable[..., Path], key: str, value: str) -> None:
    error: PackageError = _reason_of(make_package(manifest_edit=lambda m: m["slots"][0].update({key: value})))
    assert error.reason is PackageErrorReason.BAD_VALUE
    assert "slot_id" in error.detail


def test_duplicate_slot_id(make_package: Callable[..., Path], make_slot: Callable[..., dict[str, Any]]) -> None:
    error: PackageError = _reason_of(make_package(slots=[make_slot(), make_slot()]))
    assert error.reason is PackageErrorReason.DUPLICATE_SLOT
    assert error.detail == "17-03-2027_1900_uk"


def test_preview_missing_in_zip(make_package: Callable[..., Path]) -> None:
    error: PackageError = _reason_of(make_package(omit_previews={"previews/17-03-2027_1900_uk_1.jpg"}))
    assert error.reason is PackageErrorReason.PREVIEW_MISSING


def test_naive_start_is_rejected(make_package: Callable[..., Path]) -> None:
    error: PackageError = _reason_of(
        make_package(manifest_edit=lambda m: m["slots"][0].update(start="2027-03-17T19:00:00"))
    )
    assert error.reason is PackageErrorReason.BAD_VALUE
    assert "start" in error.detail


def test_read_preview_returns_bytes(make_package: Callable[..., Path]) -> None:
    package: Package = read_package(make_package())
    assert read_preview(package, "previews/17-03-2027_1900_uk_1.jpg") == b"x"
    with pytest.raises(PackageError) as raised:
        read_preview(package, "previews/nope.jpg")
    assert raised.value.reason is PackageErrorReason.PREVIEW_MISSING
