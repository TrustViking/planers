"""config\\planer.yaml + config\\channels.yaml → PlanerConfig / ChannelConfig (ТЗ §5.2).

Настройки запуска и список каналов лежат в разных файлах: planer.yaml владелец правит
редко, channels.yaml — при каждом новом канале. Язык стримов назначает оператор здесь;
язык канала на YouTube на решения не влияет.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

import yaml

from app.paths import PlanerPaths
from app.ui import messages_ru as msg


class Platform(str, Enum):
    YOUTUBE = "youtube"


class Privacy(str, Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"


CONFIG_ENCODING: Final[str] = "utf-8"
FACEBOOK_PLATFORM: Final[str] = "facebook"
CHANNEL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]+$")
DEFAULT_MIN_LEAD_MINUTES: Final[int] = 60
DEFAULT_KEEP_DAYS: Final[int] = 30
MIN_LEAD_MINUTES_MINIMUM: Final[int] = 0
KEEP_DAYS_MINIMUM: Final[int] = 1
DEFAULT_PRIVACY: Final[Privacy] = Privacy.PUBLIC
DEFAULT_AUTO_START: Final[bool] = True
DEFAULT_SET_THUMBNAIL: Final[bool] = True
DEFAULT_CATEGORY_ID: Final[str] = "22"   # People & Blogs; справочник идентификаторов — у YouTube
CHANNELS_KEY: Final[str] = "channels"
TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({"min_lead_minutes", "keep_days"})
CHANNELS_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset({CHANNELS_KEY})
CHANNEL_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "platform", "account_name", "languages", "privacy", "auto_start", "set_thumbnail", "category_id"}
)


class ConfigError(Exception):
    """Ошибка конфигурации; текст — для владельца (messages_ru), с путём ключа."""

    def __init__(self, *, config_path: Path, key_path: str, problem: str) -> None:
        super().__init__(msg.CONFIG_ERROR.format(path=config_path, key=key_path, problem=problem))
        self.config_path: Path = config_path
        self.key_path: str = key_path
        self.problem: str = problem


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    platform: Platform
    account_name: str
    languages: tuple[str, ...]
    privacy: Privacy
    auto_start: bool
    set_thumbnail: bool
    category_id: str        # категория эфира на площадке; по справочнику YouTube не проверяется


@dataclass(frozen=True)
class PlanerConfig:
    min_lead_minutes: int
    keep_days: int
    channels: tuple[ChannelConfig, ...]

    @property
    def served_languages(self) -> frozenset[str]:
        """Языки, за которые отвечает хотя бы один канал."""
        return frozenset(language for channel in self.channels for language in channel.languages)

    def channel(self, channel_key: str) -> ChannelConfig | None:
        for channel in self.channels:
            if channel.id == channel_key:
                return channel
        return None


@dataclass(frozen=True)
class _Settings:
    min_lead_minutes: int
    keep_days: int


def load_channels(path: Path) -> tuple[ChannelConfig, ...]:
    """config\\channels.yaml → каналы владельца."""
    return _ConfigParser(path).parse_channels(_read_yaml(path))


def load_planer_config(config_file: Path, channels_file: Path) -> PlanerConfig:
    settings: _Settings = _ConfigParser(config_file).parse_settings(_read_yaml(config_file))
    return PlanerConfig(
        min_lead_minutes=settings.min_lead_minutes,
        keep_days=settings.keep_days,
        channels=load_channels(channels_file),
    )


def ensure_configs_exist(paths: PlanerPaths) -> tuple[Path, ...]:
    """Возвращает файлы, только что созданные из примеров; пусто — оба уже были."""
    created: list[Path] = []
    for target, example in (
        (paths.config_file, paths.config_example),
        (paths.channels_file, paths.channels_example),
    ):
        if _copy_example(target, example):
            created.append(target)
    return tuple(created)


def _copy_example(target: Path, example: Path) -> bool:
    if target.exists():
        return False
    if not example.exists():
        raise ConfigError(
            config_path=target,
            key_path=msg.CONFIG_ROOT_KEY,
            problem=msg.CONFIG_PROBLEM_EXAMPLE_MISSING.format(example=example),
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, target)
    return True


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding=CONFIG_ENCODING))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ConfigError(
            config_path=path,
            key_path=msg.CONFIG_ROOT_KEY,
            problem=msg.CONFIG_PROBLEM_YAML.format(error=error),
        ) from error


def _choices(enum_type: type[Enum]) -> str:
    return ", ".join(str(member.value) for member in enum_type)


def _is_language_code(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip().lower()


class _ConfigParser:
    """Разбор словаря YAML; каждая ошибка — ConfigError с путём ключа (channels[1].languages)."""

    def __init__(self, config_path: Path) -> None:
        self._config_path: Path = config_path

    def parse_settings(self, raw: Any) -> _Settings:
        root: dict[str, Any] = self._mapping(
            raw,
            key_path=msg.CONFIG_ROOT_KEY,
            prefix="",
            allowed=TOP_LEVEL_KEYS,
        )
        return _Settings(
            min_lead_minutes=self._int(
                root,
                "min_lead_minutes",
                prefix="",
                default=DEFAULT_MIN_LEAD_MINUTES,
                minimum=MIN_LEAD_MINUTES_MINIMUM,
            ),
            keep_days=self._int(
                root,
                "keep_days",
                prefix="",
                default=DEFAULT_KEEP_DAYS,
                minimum=KEEP_DAYS_MINIMUM,
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

    def _error(self, key_path: str, problem: str) -> ConfigError:
        return ConfigError(config_path=self._config_path, key_path=key_path, problem=problem)

    def _mapping(
        self,
        raw: Any,
        *,
        key_path: str,
        prefix: str,
        allowed: frozenset[str],
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise self._error(key_path, msg.CONFIG_PROBLEM_NOT_MAPPING)
        for key in raw:
            if str(key) not in allowed:
                raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_UNKNOWN_KEY)
        return {str(key): value for key, value in raw.items()}

    def _required(self, mapping: dict[str, Any], key: str, *, prefix: str) -> Any:
        if key not in mapping:
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_MISSING_KEY)
        return mapping[key]

    def _text(self, mapping: dict[str, Any], key: str, *, prefix: str) -> str:
        value: Any = self._required(mapping, key, prefix=prefix)
        if not isinstance(value, str) or not value.strip():
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_NON_EMPTY_STRING)
        return value

    def _int(self, mapping: dict[str, Any], key: str, *, prefix: str, default: int, minimum: int) -> int:
        value: Any = mapping.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_INT_MIN.format(minimum=minimum))
        return value

    def _bool(self, mapping: dict[str, Any], key: str, *, prefix: str, default: bool) -> bool:
        value: Any = mapping.get(key, default)
        if not isinstance(value, bool):
            raise self._error(f"{prefix}{key}", msg.CONFIG_PROBLEM_BOOL)
        return value

    def _channels(self, root: dict[str, Any]) -> tuple[ChannelConfig, ...]:
        raw: Any = self._required(root, CHANNELS_KEY, prefix="")
        if not isinstance(raw, list) or not raw:
            raise self._error(CHANNELS_KEY, msg.CONFIG_PROBLEM_CHANNELS_EMPTY)
        channels: list[ChannelConfig] = []
        seen_ids: set[str] = set()
        for index, raw_channel in enumerate(raw):
            channel: ChannelConfig = self._channel(raw_channel, prefix=f"{CHANNELS_KEY}[{index}].")
            if channel.id in seen_ids:
                raise self._error(
                    f"{CHANNELS_KEY}[{index}].id",
                    msg.CONFIG_PROBLEM_CHANNEL_ID_DUPLICATE.format(value=channel.id),
                )
            seen_ids.add(channel.id)
            channels.append(channel)
        return tuple(channels)

    def _channel(self, raw: Any, *, prefix: str) -> ChannelConfig:
        mapping: dict[str, Any] = self._mapping(
            raw,
            key_path=prefix.rstrip("."),
            prefix=prefix,
            allowed=CHANNEL_KEYS,
        )
        return ChannelConfig(
            id=self._channel_id(mapping, prefix=prefix),
            platform=self._platform(mapping, prefix=prefix),
            account_name=self._text(mapping, "account_name", prefix=prefix),
            languages=self._languages(mapping, prefix=prefix),
            privacy=self._privacy(mapping, prefix=prefix),
            auto_start=self._bool(mapping, "auto_start", prefix=prefix, default=DEFAULT_AUTO_START),
            set_thumbnail=self._bool(mapping, "set_thumbnail", prefix=prefix, default=DEFAULT_SET_THUMBNAIL),
            category_id=self._category_id(mapping, prefix=prefix),
        )

    def _channel_id(self, mapping: dict[str, Any], *, prefix: str) -> str:
        value: str = self._text(mapping, "id", prefix=prefix)
        if not CHANNEL_ID_PATTERN.fullmatch(value):
            raise self._error(f"{prefix}id", msg.CONFIG_PROBLEM_CHANNEL_ID)
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
                msg.CONFIG_PROBLEM_PLATFORM_UNKNOWN.format(value=value, allowed=_choices(Platform)),
            ) from None

    def _languages(self, mapping: dict[str, Any], *, prefix: str) -> tuple[str, ...]:
        """Коды языков со справочником не сверяются: язык назначает оператор."""
        value: Any = self._required(mapping, "languages", prefix=prefix)
        key_path: str = f"{prefix}languages"
        if not isinstance(value, list) or not value or not all(_is_language_code(item) for item in value):
            raise self._error(key_path, msg.CONFIG_PROBLEM_LANGUAGES)
        duplicates: list[str] = sorted({item for item in value if value.count(item) > 1})
        if duplicates:
            raise self._error(key_path, msg.CONFIG_PROBLEM_LANGUAGE_DUPLICATE.format(value=duplicates[0]))
        return tuple(value)

    def _category_id(self, mapping: dict[str, Any], *, prefix: str) -> str:
        """Справочника категорий у планера нет: неизвестный id вернёт ошибку площадки."""
        value: Any = mapping.get("category_id", DEFAULT_CATEGORY_ID)
        if not isinstance(value, str) or not value.strip():
            raise self._error(f"{prefix}category_id", msg.CONFIG_PROBLEM_NON_EMPTY_STRING)
        return value

    def _privacy(self, mapping: dict[str, Any], *, prefix: str) -> Privacy:
        value: Any = mapping.get("privacy", DEFAULT_PRIVACY.value)
        try:
            return Privacy(value)
        except ValueError:
            raise self._error(
                f"{prefix}privacy",
                msg.CONFIG_PROBLEM_CHOICE.format(allowed=_choices(Privacy)),
            ) from None
