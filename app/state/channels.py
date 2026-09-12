"""Привязки каналов state\\channels.json (ТЗ §5.3): какой YouTube-канал за каким токеном.

Защита от «авторизовался не тем аккаунтом»: после первой авторизации ключ канала из
channels.yaml намертво связан с youtube_channel_id. Только runtime-данные, запись атомарная.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from app.core.dates import format_datetime_text, parse_datetime_text

CHANNELS_SCHEMA_VERSION: Final[int] = 1
CHANNELS_ENCODING: Final[str] = "utf-8"
JSON_INDENT: Final[int] = 2
TEMP_SUFFIX: Final[str] = ".tmp"
BINDINGS_SECTION: Final[str] = "channels"


class ChannelsStateError(Exception):
    """Файл привязок не читается: битый JSON, неизвестная версия, неверная структура."""


@dataclass(frozen=True)
class ChannelBinding:
    channel_key: str          # id канала из config\channels.yaml
    youtube_channel_id: str   # настоящий id канала на YouTube
    title: str                # название канала на YouTube на момент авторизации
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
            raise ChannelsStateError(f"channels state {BINDINGS_SECTION} is not an object: {path}")
        return cls(
            bindings={
                str(key): _binding_from_json(str(key), raw, path=path) for key, raw in section.items()
            }
        )

    def save(self, path: Path) -> None:
        payload: dict[str, Any] = {
            "schema_version": CHANNELS_SCHEMA_VERSION,
            BINDINGS_SECTION: {
                key: _binding_to_json(binding) for key, binding in self._bindings.items()
            },
        }
        _write_atomically(path, json.dumps(payload, ensure_ascii=False, indent=JSON_INDENT) + "\n")

    def get(self, channel_key: str) -> ChannelBinding | None:
        return self._bindings.get(channel_key)

    def upsert(self, binding: ChannelBinding) -> None:
        self._bindings[binding.channel_key] = binding


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding=CHANNELS_ENCODING))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChannelsStateError(f"channels state unreadable: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ChannelsStateError(f"channels state root is not an object: {path}")
    version: Any = payload.get("schema_version")
    if type(version) is not int or version != CHANNELS_SCHEMA_VERSION:
        raise ChannelsStateError(f"unsupported channels state schema_version={version!r}: {path}")
    return payload


def _text(raw: dict[str, Any], name: str, *, where: str) -> str:
    value: Any = raw.get(name)
    if not isinstance(value, str) or not value:
        raise ChannelsStateError(f"channel binding {where}: {name}={value!r}")
    return value


def _binding_from_json(channel_key: str, raw: Any, *, path: Path) -> ChannelBinding:
    where: str = f"{channel_key} ({path})"
    if not isinstance(raw, dict):
        raise ChannelsStateError(f"channel binding {where} is not an object")
    authorized_text: str = _text(raw, "authorized_at", where=where)
    try:
        authorized_at: datetime = parse_datetime_text(authorized_text)
    except ValueError as error:
        raise ChannelsStateError(f"channel binding {where}: authorized_at={authorized_text!r}") from error
    return ChannelBinding(
        channel_key=channel_key,
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
        with os.fdopen(file_descriptor, "w", encoding=CHANNELS_ENCODING, newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
