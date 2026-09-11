"""Журнал state\\registry.json (ТЗ §5.4): какие ключи получены и отправлена ли форма.

Не источник истины об эфирах — истина на YouTube (ТЗ §7.3). Запись атомарная.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from app.core.dates import format_datetime_text, parse_datetime_text

REGISTRY_SCHEMA_VERSION: Final[int] = 1
REGISTRY_ENCODING: Final[str] = "utf-8"
JSON_INDENT: Final[int] = 2
KEY_SEPARATOR: Final[str] = "|"
TEMP_SUFFIX: Final[str] = ".tmp"
REQUIRED_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "slot_id",
    "channel_id",
    "account_name",
    "language",
    "date",
    "time",
    "package_id",
)
OPTIONAL_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "broadcast_id",
    "broadcast_url",
    "stream_url",
    "stream_key",
    "last_error",
)


class FormStatus(str, Enum):
    PENDING = "pending"  # ключ есть, отправка формы не подтверждена — повторить
    SENT = "sent"        # подтверждено


class RegistryError(Exception):
    """Журнал не читается: битый JSON, неизвестная версия, неверная структура."""


@dataclass(frozen=True)
class Registration:
    """Поля и их порядок — как в ТЗ §5.4."""

    slot_id: str
    channel_id: str
    account_name: str
    language: str
    date: str
    time: str
    broadcast_id: str | None
    broadcast_url: str | None
    stream_url: str | None
    stream_key: str | None
    package_id: str
    created_at: datetime | None
    form_status: FormStatus
    form_sent_at: datetime | None
    previous_broadcast_ids: list[str]
    last_error: str | None


@dataclass(frozen=True)
class PackageSeen:
    file: str
    first_seen: datetime


class Registry:
    def __init__(
        self,
        registrations: Mapping[str, Registration] | None = None,
        packages: Mapping[str, PackageSeen] | None = None,
    ) -> None:
        self._registrations: dict[str, Registration] = dict(registrations or {})
        self._packages: dict[str, PackageSeen] = dict(packages or {})

    @property
    def registrations(self) -> Mapping[str, Registration]:
        return MappingProxyType(self._registrations)

    @property
    def packages(self) -> Mapping[str, PackageSeen]:
        return MappingProxyType(self._packages)

    @staticmethod
    def key(slot_id: str, channel_id: str) -> str:
        return f"{slot_id}{KEY_SEPARATOR}{channel_id}"

    @classmethod
    def load(cls, path: Path) -> Registry:
        """Нет файла → пустой журнал; schema_version != 1 → RegistryError."""
        if not path.exists():
            return cls()
        payload: dict[str, Any] = _read_payload(path)
        return cls(
            registrations={
                key: _registration_from_json(raw, where=key)
                for key, raw in _section(payload, "registrations").items()
            },
            packages={
                package_id: _package_from_json(raw, where=package_id)
                for package_id, raw in _section(payload, "packages").items()
            },
        )

    def save(self, path: Path) -> None:
        payload: dict[str, Any] = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registrations": {
                key: _registration_to_json(registration)
                for key, registration in self._registrations.items()
            },
            "packages": {
                package_id: {"file": seen.file, "first_seen": format_datetime_text(seen.first_seen)}
                for package_id, seen in self._packages.items()
            },
        }
        _write_atomically(path, json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT) + "\n")

    def get(self, key: str) -> Registration | None:
        return self._registrations.get(key)

    def upsert(self, registration: Registration) -> None:
        self._registrations[self.key(registration.slot_id, registration.channel_id)] = registration

    def mark_form_sent(self, key: str, sent_at: datetime) -> None:
        current: Registration = self._require(key)
        self._registrations[key] = replace(current, form_status=FormStatus.SENT, form_sent_at=sent_at)

    def mark_recreated(
        self,
        key: str,
        *,
        broadcast_id: str,
        broadcast_url: str,
        stream_url: str,
        stream_key: str,
        created_at: datetime,
    ) -> None:
        """Эфир пересоздан (ТЗ §7.3): старый broadcast_id — в историю, форма снова pending."""
        current: Registration = self._require(key)
        previous_ids: list[str] = list(current.previous_broadcast_ids)
        if current.broadcast_id:
            previous_ids.append(current.broadcast_id)
        self._registrations[key] = replace(
            current,
            broadcast_id=broadcast_id,
            broadcast_url=broadcast_url,
            stream_url=stream_url,
            stream_key=stream_key,
            created_at=created_at,
            form_status=FormStatus.PENDING,
            form_sent_at=None,
            previous_broadcast_ids=previous_ids,
        )

    def note_package(self, package_id: str, file: str, seen_at: datetime) -> None:
        """Запомнить пакет; уже известный не трогается (first_seen не перезаписывается)."""
        if package_id in self._packages:
            return
        self._packages[package_id] = PackageSeen(file=file, first_seen=seen_at)

    def _require(self, key: str) -> Registration:
        registration: Registration | None = self._registrations.get(key)
        if registration is None:
            raise RegistryError(f"unknown registration key={key}")
        return registration


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding=REGISTRY_ENCODING))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RegistryError(f"registry unreadable: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise RegistryError(f"registry root is not an object: {path}")
    version: Any = payload.get("schema_version")
    if type(version) is not int or version != REGISTRY_SCHEMA_VERSION:
        raise RegistryError(f"unsupported registry schema_version={version!r}: {path}")
    return payload


def _section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    section: Any = payload.get(name, {})
    if not isinstance(section, dict):
        raise RegistryError(f"registry {name} is not an object")
    return section


def _require_field(raw: dict[str, Any], name: str, where: str) -> Any:
    if name not in raw:
        raise RegistryError(f"registration {where}: missing {name}")
    return raw[name]


def _text_field(raw: dict[str, Any], name: str, where: str, *, optional: bool) -> str | None:
    value: Any = _require_field(raw, name, where)
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise RegistryError(f"registration {where}: {name}={value!r}")
    return value


def _datetime_field(raw: dict[str, Any], name: str, where: str) -> datetime | None:
    value: str | None = _text_field(raw, name, where, optional=True)
    if value is None:
        return None
    try:
        return parse_datetime_text(value)
    except ValueError as error:
        raise RegistryError(f"registration {where}: {name}={value!r}") from error


def _registration_from_json(raw: Any, *, where: str) -> Registration:
    if not isinstance(raw, dict):
        raise RegistryError(f"registration {where} is not an object")
    try:
        form_status: FormStatus = FormStatus(_require_field(raw, "form_status", where))
    except ValueError as error:
        raise RegistryError(f"registration {where}: bad form_status") from error
    previous_ids: Any = _require_field(raw, "previous_broadcast_ids", where)
    if not isinstance(previous_ids, list) or not all(isinstance(item, str) for item in previous_ids):
        raise RegistryError(f"registration {where}: bad previous_broadcast_ids")
    texts: dict[str, Any] = {name: _text_field(raw, name, where, optional=False) for name in REQUIRED_TEXT_FIELDS}
    optional_texts: dict[str, Any] = {
        name: _text_field(raw, name, where, optional=True) for name in OPTIONAL_TEXT_FIELDS
    }
    return Registration(
        **texts,
        **optional_texts,
        created_at=_datetime_field(raw, "created_at", where),
        form_status=form_status,
        form_sent_at=_datetime_field(raw, "form_sent_at", where),
        previous_broadcast_ids=list(previous_ids),
    )


def _package_from_json(raw: Any, *, where: str) -> PackageSeen:
    if not isinstance(raw, dict):
        raise RegistryError(f"package {where} is not an object")
    first_seen: datetime | None = _datetime_field(raw, "first_seen", where)
    file_name: str | None = _text_field(raw, "file", where, optional=False)
    if first_seen is None or file_name is None:
        raise RegistryError(f"package {where}: file and first_seen are required")
    return PackageSeen(file=file_name, first_seen=first_seen)


def _optional_datetime_text(value: datetime | None) -> str | None:
    return None if value is None else format_datetime_text(value)


def _registration_to_json(registration: Registration) -> dict[str, Any]:
    """Ключи — в порядке ТЗ §5.4."""
    return {
        "slot_id": registration.slot_id,
        "channel_id": registration.channel_id,
        "account_name": registration.account_name,
        "language": registration.language,
        "date": registration.date,
        "time": registration.time,
        "broadcast_id": registration.broadcast_id,
        "broadcast_url": registration.broadcast_url,
        "stream_url": registration.stream_url,
        "stream_key": registration.stream_key,
        "package_id": registration.package_id,
        "created_at": _optional_datetime_text(registration.created_at),
        "form_status": registration.form_status.value,
        "form_sent_at": _optional_datetime_text(registration.form_sent_at),
        "previous_broadcast_ids": list(registration.previous_broadcast_ids),
        "last_error": registration.last_error,
    }


def _write_atomically(path: Path, text: str) -> None:
    """temp-файл в той же папке → os.replace: читатель видит либо старый, либо новый журнал."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=TEMP_SUFFIX)
    temp_path: Path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding=REGISTRY_ENCODING, newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
