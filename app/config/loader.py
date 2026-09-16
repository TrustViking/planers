"""secrets\\planer.json + secrets\\channels.json → PlanerConfig (ТЗ §5.2).

Два файла, оба JSON, все поля обязательные, умолчаний в коде нет. channels.json заполняет
владелец: только шесть полей канала; ключ канала — ник (handle). Переписывает channels.json планер
только при выравнивании ника и названия по id YouTube (app/platforms/channel_sync.py) — функцией
save_channels_file, текст — только render_channels_file. planer.json — технический, поставляется со сборкой
заполненным и действует на все каналы. Язык стримов назначает оператор в channels.json;
язык канала на YouTube на решения не влияет. Файла нет — это ConfigError, копирования
примеров нет: шаблон печатает main.
"""
from __future__ import annotations

import json
import shutil
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from app.core.text import CONTROL_CHAR_LIMIT, HANDLE_PREFIX, UNICODE_FORM, normalize_handle
from app.paths import write_text_atomically
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
YOUTUBE_PAUSE_SECONDS_MINIMUM: Final[int] = 0
KEEP_DAYS_MINIMUM: Final[int] = 1
CHANNELS_KEY: Final[str] = "channels"
SETTINGS_KEYS: Final[tuple[str, ...]] = (
    "min_lead_minutes",
    "keep_days",
    "auto_start",
    "set_thumbnail",
    "category_id",
    "youtube_pause_seconds",
)
CHANNELS_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (CHANNELS_KEY,)
CHANNEL_KEYS: Final[tuple[str, ...]] = ("platform", "account_name", "handle", "google_account", "languages", "privacy")
# Как render_channels_file раскладывает канал: первая строка — эти поля, вторая — остальные.
CHANNEL_FIRST_LINE_KEYS: Final[tuple[str, ...]] = CHANNEL_KEYS[:4]
CHANNEL_SECOND_LINE_KEYS: Final[tuple[str, ...]] = CHANNEL_KEYS[4:]
CHANNELS_FILE_HEAD: Final[str] = '{\n  "channels": [\n'
CHANNELS_FILE_TAIL: Final[str] = "\n  ]\n}\n"
CHANNEL_LINES: Final[str] = "    {{{first},\n     {second}}}"
CHANNEL_FIELD: Final[str] = "{key}: {value}"
CHANNEL_FIELD_JOINER: Final[str] = ", "
CHANNEL_JOINER: Final[str] = ",\n"
AUTH_ALL: Final[str] = "all"  # --auth all: все каналы из channels.json
# handle — ник канала на YouTube, ключ канала: из него строится имя файла токена (core.text.token_file_stem).
HANDLE_MIN_CHARS: Final[int] = 3
HANDLE_MAX_CHARS: Final[int] = 30
HANDLE_FORBIDDEN_CHARS: Final[str] = '<>:"/\\|?*'
# account_name — название канала для людей и формы; одинаковые названия у разных каналов допустимы.
ACCOUNT_NAME_EDGE_CHAR: Final[str] = " "   # YouTube не отдаёт названия с пробелом по краю: такое не совпадёт
# google_account — подсказка аккаунта при входе, а не проверка почты: ровно один «@», части непустые, без пробелов.
GOOGLE_ACCOUNT_SEPARATOR: Final[str] = "@"
ACCOUNT_NAME_MAX_CHARS: Final[int] = 100   # предел названия канала на YouTube


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
    """Ровно шесть полей channels.json. handle — ник канала как в файле: файл токена, --auth;
    key — его единственная нормализация, ключ всех словарей и кешей по каналу.
    account_name — название канала для людей и формы.

    google_account — почта аккаунта Google канала: подсказка браузеру при входе.
    """

    platform: Platform
    account_name: str
    handle: str
    google_account: str
    languages: tuple[str, ...]
    privacy: Privacy

    @property
    def key(self) -> str:
        return normalize_handle(self.handle)


@dataclass(frozen=True)
class PlanerSettings:
    """Шесть полей planer.json: действуют на все каналы."""

    min_lead_minutes: int
    keep_days: int
    auto_start: bool
    set_thumbnail: bool
    category_id: str      # категория эфира на площадке; по справочнику YouTube не проверяется
    youtube_pause_seconds: int   # наименьший промежуток между любыми двумя обращениями к YouTube


@dataclass(frozen=True)
class PlanerConfig:
    settings: PlanerSettings
    channels: tuple[ChannelConfig, ...]

    @property
    def served_languages(self) -> frozenset[str]:
        """Языки, за которые отвечает хотя бы один канал."""
        return frozenset(language for channel in self.channels for language in channel.languages)

    def channel_by_handle(self, handle: str) -> ChannelConfig | None:
        """Ник из командной строки (--auth, пробники) — с «@» или без, в любом регистре."""
        wanted: str = normalize_handle(handle)
        for channel in self.channels:
            if channel.key == wanted:
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


def render_channels_file(channels: Iterable[ChannelConfig]) -> str:
    """Текст channels.json в том виде, в каком его пишет владелец: канал — две строки."""
    body: str = CHANNEL_JOINER.join(_channel_lines(channel) for channel in channels)
    return CHANNELS_FILE_HEAD + body + CHANNELS_FILE_TAIL


def save_channels_file(channels_file: Path, previous_file: Path, channels: Iterable[ChannelConfig]) -> None:
    """Прежний файл байт в байт — в previous_file (перезаписывается), новый — атомарно. Сбой — OSError."""
    text: str = render_channels_file(channels)
    shutil.copyfile(channels_file, previous_file)
    write_text_atomically(channels_file, text, CONFIG_ENCODING)


def _channel_lines(channel: ChannelConfig) -> str:
    values: dict[str, Any] = {
        "platform": channel.platform.value,
        "account_name": channel.account_name,
        "handle": channel.handle,
        "google_account": channel.google_account,
        "languages": list(channel.languages),
        "privacy": channel.privacy.value,
    }
    return CHANNEL_LINES.format(
        first=_channel_fields(values, CHANNEL_FIRST_LINE_KEYS),
        second=_channel_fields(values, CHANNEL_SECOND_LINE_KEYS),
    )


def _channel_fields(values: dict[str, Any], keys: tuple[str, ...]) -> str:
    return CHANNEL_FIELD_JOINER.join(
        CHANNEL_FIELD.format(key=json.dumps(key), value=json.dumps(values[key], ensure_ascii=False)) for key in keys
    )


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
    """Одна форма Unicode (NFC): «й» одним символом и «и» + знак — одно название."""
    return unicodedata.normalize(UNICODE_FORM, value)


def handle_problem(value: str) -> str | None:
    """None — ник годится; иначе что именно не так. value — уже в NFC.

    Той же проверкой проходит ник, который планер сам пишет в channels.json при выравнивании.
    """
    if not value.startswith(HANDLE_PREFIX):
        return msg.CONFIG_PROBLEM_HANDLE_PREFIX.format(value=value, prefix=HANDLE_PREFIX)
    body: str = value[len(HANDLE_PREFIX):]
    if not HANDLE_MIN_CHARS <= len(body) <= HANDLE_MAX_CHARS:
        return msg.CONFIG_PROBLEM_HANDLE_LENGTH.format(
            value=value, minimum=HANDLE_MIN_CHARS, maximum=HANDLE_MAX_CHARS, length=len(body)
        )
    for char in body:
        if char.isspace() or ord(char) < CONTROL_CHAR_LIMIT or char in HANDLE_FORBIDDEN_CHARS:
            return msg.CONFIG_PROBLEM_HANDLE_CHAR.format(value=value, char=repr(char))
    return None


def account_name_problem(value: str) -> str | None:
    """None — название годится; иначе что именно не так. value — уже в NFC и непустое.

    Название в именах файлов не участвует: ограничения — только те, что есть у названий на YouTube.
    Той же проверкой проходит название, которое планер сам пишет в channels.json при выравнивании.
    """
    if len(value) > ACCOUNT_NAME_MAX_CHARS:
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_TOO_LONG.format(maximum=ACCOUNT_NAME_MAX_CHARS, length=len(value))
    if any(ord(char) < CONTROL_CHAR_LIMIT for char in value):
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_CONTROL
    if value.startswith(ACCOUNT_NAME_EDGE_CHAR) or value.endswith(ACCOUNT_NAME_EDGE_CHAR):
        return msg.CONFIG_PROBLEM_ACCOUNT_NAME_SPACE_EDGE.format(value=value)
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
            youtube_pause_seconds=self._int(
                root, "youtube_pause_seconds", prefix="", minimum=YOUTUBE_PAUSE_SECONDS_MINIMUM
            ),
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
            self._check_unique(channel, channels, prefix=prefix)
            channels.append(channel)
        return tuple(channels)

    def _check_unique(self, channel: ChannelConfig, earlier: list[ChannelConfig], *, prefix: str) -> None:
        """Ник — ключ канала: повтор без учёта регистра — ошибка. Названия могут совпадать."""
        for other in earlier:
            if other.key == channel.key:
                raise self._error(
                    f"{prefix}handle",
                    msg.CONFIG_PROBLEM_HANDLE_DUPLICATE.format(value=channel.handle, other=other.handle),
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
            handle=self._handle(mapping, prefix=prefix),
            google_account=self._google_account(mapping, prefix=prefix),
            languages=self._languages(mapping, prefix=prefix),
            privacy=self._privacy(mapping, prefix=prefix),
        )

    def _account_name(self, mapping: dict[str, Any], *, prefix: str) -> str:
        """Название приводится к NFC до проверок: дальше везде — проверка канала, форма, отчёт — только эта форма."""
        value: str = normalize_account_name(self._text(mapping, "account_name", prefix=prefix))
        problem: str | None = account_name_problem(value)
        if problem is not None:
            raise self._error(f"{prefix}account_name", problem)
        return value

    def _handle(self, mapping: dict[str, Any], *, prefix: str) -> str:
        """Ник хранится как в файле (NFC); сравнивается только через ChannelConfig.key."""
        value: str = unicodedata.normalize(UNICODE_FORM, self._text(mapping, "handle", prefix=prefix))
        problem: str | None = handle_problem(value)
        if problem is not None:
            raise self._error(f"{prefix}handle", problem)
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
