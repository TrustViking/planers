"""*.bcast (ZIP: manifest.json + previews/) → Package с проверкой манифеста (ТЗ §5.1)."""
from __future__ import annotations

import json
import zipfile
import zlib
from pathlib import Path
from typing import Any, Final

from app.core.dates import build_slot_id, parse_date, parse_datetime_text, parse_iso_start
from app.package.model import (
    FormSpec,
    Package,
    PackageError,
    PackageErrorReason,
    Slot,
)

__all__ = ["SCHEMA_VERSION_SUPPORTED", "PackageError", "PackageErrorReason", "read_package"]

SCHEMA_VERSION_SUPPORTED: Final[int] = 1
MANIFEST_NAME: Final[str] = "manifest.json"
MANIFEST_ENCODING: Final[str] = "utf-8"
FORM_FIELD_KEYS: Final[tuple[str, ...]] = (
    "language",
    "account_name",
    "date",
    "platform",
    "stream_key",
    "stream_url",
    "time",
    "broadcast_url",
    "slot_id",
)


def read_package(path: Path) -> Package:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest: dict[str, Any] = _load_manifest(archive)
            archive_names: frozenset[str] = frozenset(archive.namelist())
    except (zipfile.BadZipFile, zlib.error, OSError) as error:
        raise PackageError(PackageErrorReason.NOT_ZIP, str(error)) from error
    return _ManifestParser(path=path, archive_names=archive_names).parse(manifest)


def _load_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw: bytes = archive.read(MANIFEST_NAME)
    except KeyError as error:
        raise PackageError(PackageErrorReason.NO_MANIFEST, MANIFEST_NAME) from error
    try:
        manifest: Any = json.loads(raw.decode(MANIFEST_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError(PackageErrorReason.BAD_JSON, str(error)) from error
    if not isinstance(manifest, dict):
        raise PackageError(PackageErrorReason.BAD_JSON, "manifest root is not an object")
    return manifest


class _ManifestParser:
    """Проверка манифеста; detail ошибки — путь поля (form.fields.slot_id, slots[2].start)."""

    def __init__(self, *, path: Path, archive_names: frozenset[str]) -> None:
        self._path: Path = path
        self._archive_names: frozenset[str] = archive_names

    def parse(self, manifest: dict[str, Any]) -> Package:
        self._check_schema(manifest)
        period: dict[str, Any] = self._mapping(manifest, "period", where="period")
        return Package(
            path=self._path,
            package_id=self._text(manifest, "package_id", where="package_id"),
            generated_at=self._datetime_text(manifest, "generated_at"),
            generator=dict(self._mapping(manifest, "generator", where="generator")),
            timezone=self._text(manifest, "timezone", where="timezone"),
            period_from=self._date_text(period, "from", where="period.from"),
            period_to=self._date_text(period, "to", where="period.to"),
            form=self._form(self._mapping(manifest, "form", where="form")),
            slots=self._slots(self._require(manifest, "slots", where="slots")),
        )

    @staticmethod
    def _check_schema(manifest: dict[str, Any]) -> None:
        if "schema_version" not in manifest:
            raise PackageError(PackageErrorReason.MISSING_KEY, "schema_version")
        version: Any = manifest["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION_SUPPORTED:
            raise PackageError(PackageErrorReason.UNSUPPORTED_SCHEMA, str(version))

    @staticmethod
    def _require(mapping: dict[str, Any], key: str, *, where: str) -> Any:
        if key not in mapping:
            raise PackageError(PackageErrorReason.MISSING_KEY, where)
        return mapping[key]

    def _mapping(self, mapping: dict[str, Any], key: str, *, where: str) -> dict[str, Any]:
        value: Any = self._require(mapping, key, where=where)
        if not isinstance(value, dict):
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where} is not an object")
        return value

    def _text(self, mapping: dict[str, Any], key: str, *, where: str, allow_empty: bool = False) -> str:
        value: Any = self._require(mapping, key, where=where)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where}={value!r}")
        return value

    def _string_list(self, mapping: dict[str, Any], key: str, *, where: str) -> tuple[str, ...]:
        value: Any = self._require(mapping, key, where=where)
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where} is not a list of strings")
        return tuple(value)

    def _date_text(self, mapping: dict[str, Any], key: str, *, where: str) -> str:
        value: str = self._text(mapping, key, where=where)
        try:
            parse_date(value)
        except ValueError as error:
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where}={value!r}") from error
        return value

    def _datetime_text(self, mapping: dict[str, Any], key: str) -> Any:
        value: str = self._text(mapping, key, where=key)
        try:
            return parse_datetime_text(value)
        except ValueError as error:
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{key}={value!r}") from error

    def _form(self, form: dict[str, Any]) -> FormSpec:
        return FormSpec(
            url=self._text(form, "url", where="form.url"),
            fields=self._form_fields(self._mapping(form, "fields", where="form.fields")),
            values=self._form_values(self._mapping(form, "values", where="form.values")),
            date_format=self._text(form, "date_format", where="form.date_format"),
        )

    @staticmethod
    def _form_fields(raw: dict[str, Any]) -> dict[str, str | None]:
        for key in FORM_FIELD_KEYS:
            if key not in raw:
                raise PackageError(PackageErrorReason.MISSING_KEY, f"form.fields.{key}")
        extra_keys: list[str] = sorted(set(raw) - set(FORM_FIELD_KEYS))
        if extra_keys:
            raise PackageError(PackageErrorReason.BAD_VALUE, f"form.fields has unknown keys {extra_keys}")
        for key in FORM_FIELD_KEYS:
            value: Any = raw[key]
            if value is not None and not (isinstance(value, str) and value.strip()):
                raise PackageError(PackageErrorReason.BAD_VALUE, f"form.fields.{key}={value!r}")
        return {key: raw[key] for key in FORM_FIELD_KEYS}

    @staticmethod
    def _form_values(raw: dict[str, Any]) -> dict[str, dict[str, str]]:
        values: dict[str, dict[str, str]] = {}
        for field_name, options in raw.items():
            if not isinstance(options, dict) or not all(
                isinstance(code, str) and isinstance(text, str) and text for code, text in options.items()
            ):
                raise PackageError(PackageErrorReason.BAD_VALUE, f"form.values.{field_name}")
            values[str(field_name)] = dict(options)
        return values

    def _slots(self, raw: Any) -> tuple[Slot, ...]:
        if not isinstance(raw, list):
            raise PackageError(PackageErrorReason.BAD_VALUE, "slots is not a list")
        slots: list[Slot] = []
        seen_ids: set[str] = set()
        for index, raw_slot in enumerate(raw):
            slot: Slot = self._slot(raw_slot, where=f"slots[{index}]")
            if slot.slot_id in seen_ids:
                raise PackageError(PackageErrorReason.DUPLICATE_SLOT, slot.slot_id)
            seen_ids.add(slot.slot_id)
            slots.append(slot)
        return tuple(slots)

    def _slot(self, raw: Any, *, where: str) -> Slot:
        if not isinstance(raw, dict):
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where} is not an object")
        date_text: str = self._text(raw, "date", where=f"{where}.date")
        time_text: str = self._text(raw, "time", where=f"{where}.time")
        language: str = self._text(raw, "language", where=f"{where}.language")
        slot_id: str = self._text(raw, "slot_id", where=f"{where}.slot_id")
        self._check_slot_id(slot_id, date_text=date_text, time_text=time_text, language=language, where=where)
        return Slot(
            slot_id=slot_id,
            date=date_text,
            time=time_text,
            start=self._start(raw, where=where),
            language=language,
            title=self._text(raw, "title", where=f"{where}.title"),
            description=self._text(raw, "description", where=f"{where}.description", allow_empty=True),
            previews=self._previews(raw, where=where),
            sources=self._string_list(raw, "sources", where=f"{where}.sources"),
        )

    @staticmethod
    def _check_slot_id(slot_id: str, *, date_text: str, time_text: str, language: str, where: str) -> None:
        try:
            expected: str = build_slot_id(date_text, time_text, language)
        except ValueError as error:
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where}: {error}") from error
        if slot_id != expected:
            raise PackageError(
                PackageErrorReason.BAD_VALUE,
                f"{where}.slot_id={slot_id!r}, by date/time/language expected {expected!r}",
            )

    def _start(self, raw: dict[str, Any], *, where: str) -> Any:
        value: str = self._text(raw, "start", where=f"{where}.start")
        try:
            return parse_iso_start(value)
        except ValueError as error:
            raise PackageError(PackageErrorReason.BAD_VALUE, f"{where}.start={value!r}") from error

    def _previews(self, raw: dict[str, Any], *, where: str) -> tuple[str, ...]:
        previews: tuple[str, ...] = self._string_list(raw, "previews", where=f"{where}.previews")
        missing: list[str] = [name for name in previews if name not in self._archive_names]
        if missing:
            raise PackageError(PackageErrorReason.PREVIEW_MISSING, f"{where}: {missing[0]}")
        return previews
