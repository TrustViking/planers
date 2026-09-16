"""secrets\\planer.json + secrets\\channels.json → PlanerConfig (ТЗ §5.2).

Два файла, оба JSON, все поля обязательные, умолчаний в коде нет. channels.json заполняет
владелец: только пять полей канала. planer.json — технический, поставляется со сборкой
заполненным и действует на все каналы. Язык стримов назначает оператор в channels.json;
язык канала на YouTube на решения не влияет. Файла нет — это ConfigError, копирования
примеров нет: шаблон печатает main.
"""
from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from app.core.text import CONTROL_CHAR_LIMIT, token_file_stem
from app.google.auth import token_file_for
from app.ui import messages_ru as msg


class Platform(str, Enum):
    YOUTUBE = "youtube"


class Privacy(str, Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"


class ConfigProblem(str, Enum):
    """Что именно не так: main по нему решает, печатать ли шаблон файла."""

    FILE_MISSING = "file_missing"
    FIELD_MISSING = "field_missing"
    INVALID = "invalid"


CONFIG_ENCODING: Final[str] = "utf-8"
FACEBOOK_PLATFORM: Final[str] = "facebook"
MIN_LEAD_MINUTES_MINIMUM: Final[int] = 0
KEEP_DAYS_MINIMUM: Final[int] = 1
CHANNELS_KEY: Final[str] = "channels"
SETTINGS_KEYS: Final[tuple[str, ...]] = ("min_lead_minutes", "keep_days", "auto_start", "set_thumbnail", "category_id")
CHANNELS_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (CHANNELS_KEY,)
CHANNEL_KEYS: Final[tuple[str, ...]] = ("platform", "account_name", "google_account", "languages", "privacy")
AUTH_ALL: Final[str] = "all"  # --auth all: все каналы из channels.json; каналом с таким именем быть не может
# account_name — название канала буква в букву как на YouTube; имя файла токена из него строит
# core.text.token_file_stem, поэтому ограничений имени файла у названия нет.
ACCOUNT_NAME_EDGE_CHAR: Final[str] = " "   # YouTube не отдаёт названия с пробелом по краю: такое не совпадёт
# google_account — подсказка аккаунта при входе, а не проверка почты: ровно один «@», части непустые, без пробелов.
GOOGLE_ACCOUNT_SEPARATOR: Final[str] = "@"
# Лимит длины: имя файла токена + путь к secrets\ должны уложиться в 260 символов пути Windows.
ACCOUNT_NAME_MAX_CHARS: Final[int] = 100
ACCOUNT_NAME_UNICODE_FORM: Final[str] = "NFC"


class ConfigError(Exception):
    """Ошибка конфигурации; текст — для владельца (messages_ru), с путём поля."""

    def __init__(
        self,
        *,
        config_path: Path,
        key_path: str,
        problem: str,
        kind: ConfigProblem = ConfigProblem.INVALID,
    ) -> None:
        super().__init__(msg.CONFIG_ERROR.format(path=config_path, key=key_path, problem=problem))
        self.config_path: Path = config_path
        self.key_path: str = key_path
        self.problem: str = problem
        self.kind: ConfigProblem = kind

    @property
    def is_template_needed(self) -> bool:
        """Нет файла или поля — владельцу нужен точный шаблон файла."""
        return self.kind in (ConfigProblem.FILE_MISSING, ConfigProblem.FIELD_MISSING)


@dataclass(frozen=True)
class ChannelConfig:
    """Ровно пять полей channels.json. account_name — название канала как на YouTube: проверка канала,
    форма, логи, отчёт; имя файла токена строится из него (core.text.token_file_stem).

    google_account — почта аккаунта Google канала: подсказка браузеру при входе.
    """

    platform: Platform
    account_name: str
    google_account: str
    languages: tuple[str, ...]
    privacy: Privacy


@dataclass(frozen=True)
class PlanerSettings:
    """Пять полей planer.json: действуют на все каналы."""

    min_lead_minutes: int
    keep_days: int
    auto_start: bool
    set_thumbnail: bool
    category_id: str      # категория эфира на площадке; по справочнику YouTube не проверяется


@dataclass(frozen=True)
class PlanerConfig:
    settings: PlanerSettings
    channels: tuple[ChannelConfig, ...]

    @property
    def served_languages(self) -> frozenset[str]:
        """Языки, за которые отвечает хотя бы один канал."""
        return frozenset(language for channel in self.channels for language in channel.languages)

    def channel(self, account_name: str) -> ChannelConfig | None:
        """Имя из командной строки (--auth) приводится к той же форме, что и имена из конфига."""
        wanted: str = normalize_account_name(account_name)
        for channel in self.channels:
            if channel.account_name == wanted:
                return channel
        return None


def load_settings(path: Path) -> PlanerSettings:
    """secrets\\planer.json → настройки планера."""
    return _ConfigParser(path).parse_settings(_read_json(path))


def load_channels(path: Path) -> tuple[ChannelConfig, ...]:
    """secrets\\channels.json → каналы владельца."""
    return _ConfigParser(path).parse_channels(_read_json(path))


def load_planer_config(settings_file: Path, channels_file: Path) -> PlanerConfig:
    """Каналы читаются первыми: их владелец заполняет сам, ошибка в них вероятнее."""
    channels: tuple[ChannelConfig, ...] = load_channels(channels_file)
    return PlanerConfig(settings=load_settings(settings_file), channels=channels)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(
            config_path=path,
            key_path=msg.CONFIG_ROOT_KEY,
            problem=msg.CONFIG_PROBLEM_FILE_MISSING,
            kind=ConfigProblem.FILE_MISSING,
        )
    try:
        return json.loads(path.read_text(encoding=CONFIG_ENCODING), object_pairs_hook=_UniquePairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(
            config_path=path,
            key_path=msg.CONFIG_ROOT_KEY,
            problem=msg.CONFIG_PROBLEM_JSON.format(error=error),
        ) from error


class _UniquePairs(dict[str, Any]):
    """Объект JSON с повтором поля: json молча взял бы последнее значение."""

    def __init__(self, pairs: Iterable[tuple[str, Any]]) -> None:
        super().__init__()
        self.duplicates: list[str] = []
        for key, value in pairs:
            if key in self:
                self.duplicates.append(key)
            self[key] = value


def allowed_values(enum_type: type[Enum]) -> str:
    """Допустимые значения поля — и для ошибки, и для подсказки к шаблону channels.json."""
    return ", ".join(str(member.value) for member in enum_type)


def _is_language_code(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip().lower()


def normalize_account_name(value: str) -> str:
    """Одна форма Unicode (NFC): «й» одним символом и «и» + знак — одно имя и один токен."""
    return unicodedata.normalize(ACCOUNT_NAME_UNICODE_FORM, value)


def _account_name_problem(value: str) -> str | None:
    """None — название годится; иначе что именно не так. value — уже в NFC; непустоту проверил _text.

    Символы, недопустимые в имени файла, — законная часть названия: их заменяет token_file_stem.
    """
    if len(value) > ACCOUNT_NAME_MAX_CHARS:
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_TOO_LONG.format(maximum=ACCOUNT_NAME_MAX_CHARS, length=len(value))
    if any(ord(char) < CONTROL_CHAR_LIMIT for char in value):
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_CONTROL
    if value.startswith(ACCOUNT_NAME_EDGE_CHAR) or value.endswith(ACCOUNT_NAME_EDGE_CHAR):
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_SPACE_EDGE.format(value=value)
    if value.casefold() == AUTH_ALL.casefold():
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_AUTH_ALL.format(value=value, auth_all=AUTH_ALL)
    return None


def _is_google_account(value: str) -> bool:
    """Минимальная честная проверка: local@domain, обе части непустые, пробелов нет."""
    local, separator, domain = value.partition(GOOGLE_ACCOUNT_SEPARATOR)
    if not separator or not local or not domain or GOOGLE_ACCOUNT_SEPARATOR in domain:
        return False
    return not any(char.isspace() for char in value)


class _ConfigParser:
    """Разбор объекта JSON; каждая ошибка — ConfigError с путём поля (channels[1].languages)."""

    def __init__(self, config_path: Path) -> None:
        self._config_path: Path = config_path

    def parse_settings(self, raw: Any) -> PlanerSettings:
        root: dict[str, Any] = self._mapping(raw, key_path=msg.CONFIG_ROOT_KEY, prefix="", allowed=SETTINGS_KEYS)
        return PlanerSettings(
            min_lead_minutes=self._int(root, "min_lead_minutes", prefix="", minimum=MIN_LEAD_MINUTES_MINIMUM),
            keep_days=self._int(root, "keep_days", prefix="", minimum=KEEP_DAYS_MINIMUM),
            auto_start=self._bool(root, "auto_start", prefix=""),
            set_thumbnail=self._bool(root, "set_thumbnail", prefix=""),
            category_id=self._text(root, "category_id", prefix=""),
        )

    def parse_channels(self, raw: Any) -> tuple[ChannelConfig, ...]:
        root: dict[str, Any] = self._mapping(
            raw,
            key_path=msg.CONFIG_ROOT_KEY,
            prefix="",
            allowed=CHANNELS_TOP_LEVEL_KEYS,
        )
        return self._channels(root)

    def _error(self, key_path: str, problem: str, kind: ConfigProblem = ConfigProblem.INVALID) -> ConfigError:
        return ConfigError(config_path=self._config_path, key_path=key_path, problem=problem, kind=kind)

    def _mapping(
        self,
        raw: Any,
        *,
        key_path: str,
        prefix: str,
        allowed: tuple[str, ...],
    ) -> dict[str, Any]:
        """Неизвестное или повторённое поле — ошибка с его именем; отсутствующие проверяются следом."""
        if not isinstance(raw, dict):
            raise self._error(key_path, msg.CONFIG_PROBLEM_NOT_MAPPING)
        for key in raw:
            if key not in allowed:
                raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_UNKNOWN_KEY)
        duplicates: list[str] = getattr(raw, "duplicates", [])
        if duplicates:
            raise self._error(f"{prefix}{duplicates[0]}", msg.CONFIG_PROBLEM_DUPLICATE_KEY)
        for key in allowed:
            if key not in raw:
                raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_MISSING_KEY, ConfigProblem.FIELD_MISSING)
        return dict(raw)

    def _text(self, mapping: dict[str, Any], key: str, *, prefix: str) -> str:
        value: Any = mapping[key]
        if not isinstance(value, str) or not value.strip():
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_NON_EMPTY_STRING)
        return value

    def _int(self, mapping: dict[str, Any], key: str, *, prefix: str, minimum: int) -> int:
        value: Any = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_INT_MIN.format(minimum=minimum))
        return value

    def _bool(self, mapping: dict[str, Any], key: str, *, prefix: str) -> bool:
        value: Any = mapping[key]
        if not isinstance(value, bool):
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_BOOL)
        return value

    def _channels(self, root: dict[str, Any]) -> tuple[ChannelConfig, ...]:
        raw: Any = root[CHANNELS_KEY]
        if not isinstance(raw, list) or not raw:
            raise self._error(CHANNELS_KEY, msg.CONFIG_PROBLEM_CHANNELS_EMPTY)
        channels: list[ChannelConfig] = []
        for index, raw_channel in enumerate(raw):
            prefix: str = f"{CHANNELS_KEY}[{index}]."
            channel: ChannelConfig = self._channel(raw_channel, prefix=prefix)
            self._check_unique(channel.account_name, channels, prefix=prefix)
            channels.append(channel)
        return tuple(channels)

    def _check_unique(self, name: str, earlier: list[ChannelConfig], *, prefix: str) -> None:
        """Один файл токена — один канал: Windows не различает регистр в имени файла."""
        stem: str = token_file_stem(name).casefold()
        for other in earlier:
            if other.account_name.casefold() == name.casefold():
                raise self._error(
                    f"{prefix}account_name", msg.CONFIG_PROBLEM_ACCOUNT_NAME_DUPLICATE.format(value=name)
                )
            if token_file_stem(other.account_name).casefold() == stem:
                raise self._error(
                    f"{prefix}account_name",
                    msg.CONFIG_PROBLEM_TOKEN_FILE_COLLISION.format(
                        first=other.account_name, second=name, file_name=token_file_for(Path(), name).name
                    ),
                )

    def _channel(self, raw: Any, *, prefix: str) -> ChannelConfig:
        mapping: dict[str, Any] = self._mapping(
            raw,
            key_path=prefix.rstrip("."),
            prefix=prefix,
            allowed=CHANNEL_KEYS,
        )
        return ChannelConfig(
            platform=self._platform(mapping, prefix=prefix),
            account_name=self._account_name(mapping, prefix=prefix),
            google_account=self._google_account(mapping, prefix=prefix),
            languages=self._languages(mapping, prefix=prefix),
            privacy=self._privacy(mapping, prefix=prefix),
        )

    def _account_name(self, mapping: dict[str, Any], *, prefix: str) -> str:
        """Название приводится к NFC до проверок: дальше везде — проверка канала, токен, отчёт — только эта форма."""
        value: str = normalize_account_name(self._text(mapping, "account_name", prefix=prefix))
        problem: str | None = _account_name_problem(value)
        if problem is not None:
            raise self._error(f"{prefix}account_name", problem)
        return value

    def _google_account(self, mapping: dict[str, Any], *, prefix: str) -> str:
        value: str = self._text(mapping, "google_account", prefix=prefix)
        if not _is_google_account(value):
            raise self._error(f"{prefix}google_account", msg.CONFIG_PROBLEM_GOOGLE_ACCOUNT.format(value=value))
        return value

    def _platform(self, mapping: dict[str, Any], *, prefix: str) -> Platform:
        value: str = self._text(mapping, "platform", prefix=prefix)
        if value == FACEBOOK_PLATFORM:
            raise self._error(f"{prefix}platform", msg.CONFIG_PROBLEM_PLATFORM_FACEBOOK)
        try:
            return Platform(value)
        except ValueError:
            raise self._error(
                f"{prefix}platform",
                msg.CONFIG_PROBLEM_PLATFORM_UNKNOWN.format(value=value, allowed=allowed_values(Platform)),
            ) from None

    def _languages(self, mapping: dict[str, Any], *, prefix: str) -> tuple[str, ...]:
        """Коды языков со справочником не сверяются: язык назначает оператор."""
        value: Any = mapping["languages"]
        key_path: str = f"{prefix}languages"
        if not isinstance(value, list) or not value or not all(_is_language_code(item) for item in value):
            raise self._error(key_path, msg.CONFIG_PROBLEM_LANGUAGES)
        duplicates: list[str] = sorted({item for item in value if value.count(item) > 1})
        if duplicates:
            raise self._error(key_path, msg.CONFIG_PROBLEM_LANGUAGE_DUPLICATE.format(value=duplicates[0]))
        return tuple(value)

    def _privacy(self, mapping: dict[str, Any], *, prefix: str) -> Privacy:
        value: Any = mapping["privacy"]
        try:
            return Privacy(value)
        except ValueError:
            raise self._error(
                f"{prefix}privacy",
                msg.CONFIG_PROBLEM_CHOICE.format(allowed=allowed_values(Privacy)),
            ) from None
