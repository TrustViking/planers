"""Привязки каналов app\\state\\bindings.json (ТЗ §5.3): какой YouTube-канал за каким account_name.

Защита от «вошёл не тем аккаунтом»: при первом обращении к каналу его account_name из
channels.json намертво связывается с youtube_channel_id. Один YouTube-канал — одно имя.
Только runtime-данные, запись атомарная.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from app.core.dates import format_datetime_text, parse_datetime_text

BINDINGS_SCHEMA_VERSION: Final[int] = 1
BINDINGS_ENCODING: Final[str] = "utf-8"
JSON_INDENT: Final[int] = 2
TEMP_SUFFIX: Final[str] = ".tmp"
BINDINGS_SECTION: Final[str] = "channels"


class ChannelsStateError(Exception):
    """Файл привязок не читается: битый JSON, неизвестная версия, неверная структура."""


class BindingVerdict(str, Enum):
    NEW = "new"            # имени ещё нет, YouTube-канал свободен — записать привязку
    SAME = "same"          # имя уже привязано к этому же YouTube-каналу
    MISMATCH = "mismatch"  # имя привязано к другому YouTube-каналу
    TAKEN = "taken"        # этот YouTube-канал записан под другим именем


@dataclass(frozen=True)
class ChannelBinding:
    account_name: str         # account_name канала из config\channels.json
    youtube_channel_id: str   # настоящий id канала на YouTube
    title: str                # название канала на YouTube на момент привязки
    authorized_at: datetime


class ChannelBindings:
    def __init__(self, bindings: Mapping[str, ChannelBinding] | None = None) -> None:
        self._bindings: dict[str, ChannelBinding] = dict(bindings or {})

    @property
    def bindings(self) -> Mapping[str, ChannelBinding]:
        return MappingProxyType(self._bindings)

    @classmethod
    def load(cls, path: Path) -> ChannelBindings:
        """Нет файла → пусто; schema_version != 1 → ChannelsStateError."""
        if not path.exists():
            return cls()
        payload: dict[str, Any] = _read_payload(path)
        section: Any = payload.get(BINDINGS_SECTION, {})
        if not isinstance(section, dict):
            raise ChannelsStateError(f"bindings {BINDINGS_SECTION} is not an object: {path}")
        return cls(
            bindings={
                str(key): _binding_from_json(str(key), raw, path=path) for key, raw in section.items()
            }
        )

    def save(self, path: Path) -> None:
        payload: dict[str, Any] = {
            "schema_version": BINDINGS_SCHEMA_VERSION,
            BINDINGS_SECTION: {
                key: _binding_to_json(binding) for key, binding in self._bindings.items()
            },
        }
        _write_atomically(path, json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT) + "\n")

    def get(self, account_name: str) -> ChannelBinding | None:
        return self._bindings.get(account_name)

    def find_by_youtube_channel_id(self, youtube_channel_id: str) -> ChannelBinding | None:
        for binding in self._bindings.values():
            if binding.youtube_channel_id == youtube_channel_id:
                return binding
        return None

    def verdict(self, account_name: str, youtube_channel_id: str) -> BindingVerdict:
        """Сначала своё имя, потом чужое: переименованный канал — это TAKEN, а не NEW."""
        known: ChannelBinding | None = self.get(account_name)
        if known is not None:
            return BindingVerdict.SAME if known.youtube_channel_id == youtube_channel_id else BindingVerdict.MISMATCH
        if self.find_by_youtube_channel_id(youtube_channel_id) is not None:
            return BindingVerdict.TAKEN
        return BindingVerdict.NEW

    def upsert(self, binding: ChannelBinding) -> None:
        self._bindings[binding.account_name] = binding


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding=BINDINGS_ENCODING))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChannelsStateError(f"bindings unreadable: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ChannelsStateError(f"bindings root is not an object: {path}")
    version: Any = payload.get("schema_version")
    if type(version) is not int or version != BINDINGS_SCHEMA_VERSION:
        raise ChannelsStateError(f"unsupported bindings schema_version={version!r}: {path}")
    return payload


def _text(raw: dict[str, Any], name: str, *, where: str) -> str:
    value: Any = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ChannelsStateError(f"channel binding {where}: {name}={value!r}")
    return value


def _binding_from_json(account_name: str, raw: Any, *, path: Path) -> ChannelBinding:
    where: str = f"{account_name!r} ({path})"
    if not isinstance(raw, dict):
        raise ChannelsStateError(f"channel binding {where} is not an object")
    authorized_text: str = _text(raw, "authorized_at", where=where)
    try:
        authorized_at: datetime = parse_datetime_text(authorized_text)
    except ValueError as error:
        raise ChannelsStateError(f"channel binding {where}: authorized_at={authorized_text!r}") from error
    return ChannelBinding(
        account_name=account_name,
        youtube_channel_id=_text(raw, "youtube_channel_id", where=where),
        title=_text(raw, "title", where=where),
        authorized_at=authorized_at,
    )


def _binding_to_json(binding: ChannelBinding) -> dict[str, Any]:
    return {
        "youtube_channel_id": binding.youtube_channel_id,
        "title": binding.title,
        "authorized_at": format_datetime_text(binding.authorized_at),
    }


def _write_atomically(path: Path, text: str) -> None:
    """temp-файл рядом → os.replace: читатель видит либо старый файл, либо новый."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}_", suffix=TEMP_SUFFIX)
    temp_path: Path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding=BINDINGS_ENCODING, newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
