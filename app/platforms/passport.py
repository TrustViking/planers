"""Паспорт каналов secrets\\channels_passport.json: ник ↔ id YouTube ↔ файл токена. Пишет только планер.

Паспорт — не вход для решений об эфирах: по нему планер узнаёт, что канал с другим ником — тот же
(id YouTube не меняется никогда), и находит токен после ручной смены ника в channels.json.
Записи каналов, которых уже нет в channels.json, не удаляются. Нет файла — пустой паспорт;
файл не читается или не той формы — пустой паспорт, который перезапишет ближайшее сохранение.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Final

from app.config.loader import ChannelConfig
from app.core.text import normalize_handle
from app.observability.logging_setup import get_logger
from app.paths import write_text_atomically
from app.platforms.base import ChannelInfo, channel_url_for, handle_url_for

LOGGER = get_logger("passport")

PASSPORT_ENCODING: Final[str] = "utf-8"
PASSPORT_INDENT: Final[int] = 2
PASSPORT_KEY: Final[str] = "channels"
LIST_FIELDS: Final[frozenset[str]] = frozenset({"previous_handles", "previous_titles"})
OPTIONAL_FIELDS: Final[frozenset[str]] = frozenset({"youtube_handle_raw"})


class PassportFormatError(ValueError):
    """Файл паспорта не той формы: текст (диагностика) — что именно не так."""


@dataclass(frozen=True)
class PassportEntry:
    handle: str                     # ник как в channels.json после выравнивания
    account_name: str               # название как в channels.json после выравнивания
    youtube_channel_id: str
    youtube_title: str
    youtube_handle_raw: str | None  # snippet.customUrl как пришёл
    google_account: str
    token_file: str                 # имя файла токена в secrets\
    channel_url: str
    handle_url: str
    first_verified_at: str          # DD-MM-YYYY HH:MM, местное время
    last_verified_at: str
    previous_handles: tuple[str, ...] = ()
    previous_titles: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return normalize_handle(self.handle)


ENTRY_FIELDS: Final[tuple[str, ...]] = tuple(item.name for item in fields(PassportEntry))


class ChannelPassport:
    """Записи в памяти; save() пишет файл целиком и атомарно."""

    def __init__(self, path: Path, entries: Iterable[PassportEntry] = ()) -> None:
        self._path: Path = path
        self._entries: list[PassportEntry] = list(entries)

    @classmethod
    def load(cls, path: Path) -> tuple[ChannelPassport, str | None]:
        """Паспорт и текст проблемы чтения (None — прочитан или файла нет)."""
        if not path.is_file():
            return cls(path), None
        try:
            raw: Any = json.loads(path.read_text(encoding=PASSPORT_ENCODING))
            return cls(path, _parse_entries(raw)), None
        except (OSError, UnicodeDecodeError, ValueError) as error:
            LOGGER.warning("passport_unreadable path=%s error=%s", path, error)
            return cls(path), str(error)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entries(self) -> tuple[PassportEntry, ...]:
        return tuple(self._entries)

    def find_by_key(self, key: str) -> PassportEntry | None:
        return next((entry for entry in self._entries if entry.key == key), None)

    def find_by_channel_id(self, youtube_channel_id: str) -> PassportEntry | None:
        return next((entry for entry in self._entries if entry.youtube_channel_id == youtube_channel_id), None)

    def record(self, entry: PassportEntry) -> None:
        """Одна запись на канал: прежняя запись того же id или того же ника заменяется."""
        self._entries = [
            item
            for item in self._entries
            if item.youtube_channel_id != entry.youtube_channel_id and item.key != entry.key
        ]
        self._entries.append(entry)

    def record_verified(
        self,
        before: ChannelConfig,
        after: ChannelConfig,
        info: ChannelInfo,
        *,
        token_file: str,
        verified_at: str,
    ) -> PassportEntry:
        """Канал подтверждён (after — значения channels.json после выравнивания, before — до него)."""
        old: PassportEntry | None = self.find_by_channel_id(info.youtube_channel_id)
        entry: PassportEntry = PassportEntry(
            handle=after.handle,
            account_name=after.account_name,
            youtube_channel_id=info.youtube_channel_id,
            youtube_title=info.title,
            youtube_handle_raw=info.handle_raw,
            google_account=after.google_account,
            token_file=token_file,
            channel_url=channel_url_for(after.platform, info.youtube_channel_id),
            handle_url=handle_url_for(after.platform, after.handle),
            first_verified_at=old.first_verified_at if old is not None else verified_at,
            last_verified_at=verified_at,
            previous_handles=_previous_handles(old, before, after),
            previous_titles=_previous_titles(old, before, after),
        )
        self.record(entry)
        return entry

    def render(self) -> str:
        ordered: list[PassportEntry] = sorted(self._entries, key=lambda entry: (entry.account_name, entry.handle))
        payload: dict[str, Any] = {PASSPORT_KEY: [_entry_json(entry) for entry in ordered]}
        return json.dumps(payload, ensure_ascii=False, indent=PASSPORT_INDENT) + "\n"

    def save(self) -> str | None:
        """None — записан; иначе текст ошибки (запуск продолжается)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomically(self._path, self.render(), PASSPORT_ENCODING)
        except OSError as error:
            LOGGER.warning("passport_write_failed path=%s error=%s", self._path, error)
            return str(error)
        LOGGER.info("passport_saved path=%s channels=%d", self._path, len(self._entries))
        return None


def _previous_handles(old: PassportEntry | None, before: ChannelConfig, after: ChannelConfig) -> tuple[str, ...]:
    candidates: list[str] = [*(old.previous_handles if old else ()), *([old.handle] if old else []), before.handle]
    kept: dict[str, str] = {}
    for handle in candidates:
        if normalize_handle(handle) != after.key:
            kept.setdefault(normalize_handle(handle), handle)
    return tuple(kept.values())


def _previous_titles(old: PassportEntry | None, before: ChannelConfig, after: ChannelConfig) -> tuple[str, ...]:
    candidates: list[str] = [
        *(old.previous_titles if old else ()),
        *([old.account_name] if old else []),
        before.account_name,
    ]
    return tuple(dict.fromkeys(title for title in candidates if title != after.account_name))


def _entry_json(entry: PassportEntry) -> dict[str, Any]:
    raw: dict[str, Any] = asdict(entry)
    return {name: list(raw[name]) if name in LIST_FIELDS else raw[name] for name in ENTRY_FIELDS}


def _parse_entries(raw: Any) -> list[PassportEntry]:
    if not isinstance(raw, dict) or set(raw) != {PASSPORT_KEY} or not isinstance(raw[PASSPORT_KEY], list):
        raise PassportFormatError(f'expected {{"{PASSPORT_KEY}": [...]}}')
    return [_parse_entry(item, index) for index, item in enumerate(raw[PASSPORT_KEY])]


def _parse_entry(raw: Any, index: int) -> PassportEntry:
    if not isinstance(raw, dict) or set(raw) != set(ENTRY_FIELDS):
        raise PassportFormatError(f"{PASSPORT_KEY}[{index}]: expected exactly the fields {', '.join(ENTRY_FIELDS)}")
    values: dict[str, Any] = {}
    for name in ENTRY_FIELDS:
        values[name] = _field_value(raw[name], name, index)
    return PassportEntry(**values)


def _field_value(value: Any, name: str, index: int) -> Any:
    if name in LIST_FIELDS:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PassportFormatError(f"{PASSPORT_KEY}[{index}].{name}: expected a list of strings")
        return tuple(value)
    if value is None and name in OPTIONAL_FIELDS:
        return None
    if not isinstance(value, str) or not value:
        raise PassportFormatError(f"{PASSPORT_KEY}[{index}].{name}: expected a non-empty string")
    return value
