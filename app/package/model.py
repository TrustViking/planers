"""Пакет броадкастера в памяти (ТЗ §5.1): Package, Slot, FormSpec; ошибки чтения пакета.

PackageError живёт здесь, а не в reader.py: read_preview тоже её бросает, и так
model не импортирует reader (reader её реэкспортирует).
"""
from __future__ import annotations

import zipfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class PackageErrorReason(str, Enum):
    NOT_ZIP = "not_zip"
    NO_MANIFEST = "no_manifest"
    BAD_JSON = "bad_json"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    MISSING_KEY = "missing_key"
    BAD_VALUE = "bad_value"
    DUPLICATE_SLOT = "duplicate_slot"
    PREVIEW_MISSING = "preview_missing"


class PackageError(Exception):
    """Пакет не читается; reason — идентификатор причины, detail — что именно."""

    def __init__(self, reason: PackageErrorReason, detail: str) -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason: PackageErrorReason = reason
        self.detail: str = detail


@dataclass(frozen=True)
class FormSpec:
    url: str
    fields: dict[str, str | None]
    values: dict[str, dict[str, str]]
    date_format: str


@dataclass(frozen=True)
class Slot:
    """Один эфир из пакета. Все поля заполняются при разборе манифеста и потом не меняются."""

    slot_id: str
    date: str
    time: str
    start: datetime
    language: str
    title: str
    description: str
    previews: tuple[str, ...]
    sources: tuple[str, ...]
    form: FormSpec          # форма своего пакета: куда уйдёт ключ этого слота (ТЗ §7.5)


@dataclass(frozen=True)
class Package:
    path: Path
    package_id: str
    generated_at: datetime  # naive, местное время броадкастера — только для сортировки и отчёта
    generator: dict[str, Any]
    timezone: str
    period_from: str
    period_to: str
    form: FormSpec
    slots: tuple[Slot, ...]


def slot_order_key(slot: Slot) -> tuple[datetime, str]:
    """Порядок слотов: момент старта, затем язык (= дата, время, язык по Киеву)."""
    return (slot.start, slot.language)


def read_preview(package: Package, name: str) -> bytes:
    """Файл превью из архива — по требованию, пакет в память целиком не читается."""
    try:
        with zipfile.ZipFile(package.path) as archive:
            return archive.read(name)
    except KeyError as error:
        raise PackageError(PackageErrorReason.PREVIEW_MISSING, name) from error
    except (zipfile.BadZipFile, zlib.error, OSError) as error:
        raise PackageError(PackageErrorReason.NOT_ZIP, str(error)) from error
