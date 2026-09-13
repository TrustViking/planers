from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config.loader import (
    ConfigError,
    PlanerConfig,
    Privacy,
    ensure_configs_exist,
    load_channels,
    load_planer_config,
)
from app.paths import PlanerPaths
from app.ui import messages_ru as msg

BASE_SETTINGS: dict[str, Any] = {
    "min_lead_minutes": 60,
    "keep_days": 30,
}
BASE_CHANNELS: dict[str, Any] = {
    "channels": [
        {"id": "yt_ua", "platform": "youtube", "account_name": "Test UA", "languages": ["uk"]},
        {"id": "yt_ru", "platform": "youtube", "account_name": "Test RU", "languages": ["ru", "en"]},
    ],
}


def _write(path: Path, config: Any) -> Path:
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _write_settings(tmp_path: Path, config: Any) -> Path:
    return _write(tmp_path / "planer.yaml", config)


def _write_channels(tmp_path: Path, config: Any) -> Path:
    return _write(tmp_path / "channels.yaml", config)


def _channel(index: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda config: config["channels"][index]


INVALID_SETTINGS: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    ("owner_removed", lambda c: c.update(owner="Иван"), "owner"),
    ("min_lead_negative", lambda c: c.update(min_lead_minutes=-1), "min_lead_minutes"),
    ("min_lead_text", lambda c: c.update(min_lead_minutes="60"), "min_lead_minutes"),
    ("keep_days_zero", lambda c: c.update(keep_days=0), "keep_days"),
    ("unknown_top_key", lambda c: c.update(extra=1), "extra"),
    ("channels_in_settings", lambda c: c.update(channels=[]), "channels"),
]

INVALID_CHANNELS: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    ("channels_empty", lambda c: c.update(channels=[]), "channels"),
    ("channels_missing", lambda c: c.pop("channels"), "channels"),
    ("channel_not_mapping", lambda c: c["channels"].__setitem__(0, "yt_ua"), "channels[0]"),
    ("id_uppercase", lambda c: _channel(0)(c).update(id="YT_UA"), "channels[0].id"),
    ("id_with_dash", lambda c: _channel(0)(c).update(id="yt-ua"), "channels[0].id"),
    ("id_duplicate", lambda c: _channel(1)(c).update(id="yt_ua"), "channels[1].id"),
    ("platform_facebook", lambda c: _channel(0)(c).update(platform="facebook"), "channels[0].platform"),
    ("platform_unknown", lambda c: _channel(0)(c).update(platform="rumble"), "channels[0].platform"),
    ("platform_missing", lambda c: _channel(0)(c).pop("platform"), "channels[0].platform"),
    ("account_name_blank", lambda c: _channel(0)(c).update(account_name="  "), "channels[0].account_name"),
    ("languages_empty", lambda c: _channel(0)(c).update(languages=[]), "channels[0].languages"),
    ("languages_uppercase", lambda c: _channel(0)(c).update(languages=["UK"]), "channels[0].languages"),
    ("languages_not_list", lambda c: _channel(0)(c).update(languages="uk"), "channels[0].languages"),
    ("languages_duplicate", lambda c: _channel(1)(c).update(languages=["ru", "ru"]), "channels[1].languages"),
    ("privacy_unknown", lambda c: _channel(0)(c).update(privacy="private"), "channels[0].privacy"),
    ("auto_start_not_bool", lambda c: _channel(0)(c).update(auto_start="yes"), "channels[0].auto_start"),
    ("set_thumbnail_not_bool", lambda c: _channel(0)(c).update(set_thumbnail=1), "channels[0].set_thumbnail"),
    ("unknown_top_key", lambda c: c.update(owner="Тест"), "owner"),
    ("unknown_channel_key", lambda c: _channel(0)(c).update(token="x"), "channels[0].token"),
]


def test_repo_examples_load_together(repo_config_example: Path, repo_channels_example: Path) -> None:
    config: PlanerConfig = load_planer_config(repo_config_example, repo_channels_example)
    assert config.min_lead_minutes == 60
    assert config.keep_days == 30
    assert [channel.id for channel in config.channels] == ["yt_ua", "yt_ru"]
    assert config.channels[0].account_name == "Іван UA"
    assert config.channels[1].languages == ("ru", "en")
    assert config.channels[1].privacy is Privacy.PUBLIC
    assert config.channels[1].auto_start is True
    assert config.channels[1].set_thumbnail is True


def test_optional_keys_take_defaults(tmp_path: Path) -> None:
    settings: Path = _write_settings(tmp_path, {})
    channels: Path = _write_channels(
        tmp_path,
        {"channels": [{"id": "yt_ua", "platform": "youtube", "account_name": "Test UA", "languages": ["uk"]}]},
    )
    config: PlanerConfig = load_planer_config(settings, channels)
    assert (config.min_lead_minutes, config.keep_days) == (60, 30)
    assert config.channels[0].privacy is Privacy.PUBLIC
    assert config.served_languages == frozenset({"uk"})


def test_unknown_language_code_is_accepted(tmp_path: Path) -> None:
    """Язык назначает оператор: справочника кодов у планера нет."""
    channels: Path = _write_channels(
        tmp_path,
        {"channels": [{"id": "yt_x", "platform": "youtube", "account_name": "X", "languages": ["zz"]}]},
    )
    assert load_channels(channels)[0].languages == ("zz",)


def test_channel_lookup_by_key(repo_config_example: Path, repo_channels_example: Path) -> None:
    config: PlanerConfig = load_planer_config(repo_config_example, repo_channels_example)
    found = config.channel("yt_ru")
    assert found is not None and found.account_name == "Иван RU"
    assert config.channel("yt_nope") is None


@pytest.mark.parametrize(
    ("mutate", "key_path"),
    [case[1:] for case in INVALID_SETTINGS],
    ids=[case[0] for case in INVALID_SETTINGS],
)
def test_invalid_settings_are_rejected_with_key_path(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
    key_path: str,
) -> None:
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    mutate(settings)
    path: Path = _write_settings(tmp_path, settings)
    channels: Path = _write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS))
    with pytest.raises(ConfigError) as raised:
        load_planer_config(path, channels)
    assert raised.value.key_path == key_path
    assert str(path) in str(raised.value)


@pytest.mark.parametrize(
    ("mutate", "key_path"),
    [case[1:] for case in INVALID_CHANNELS],
    ids=[case[0] for case in INVALID_CHANNELS],
)
def test_invalid_channels_are_rejected_with_key_path(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
    key_path: str,
) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    mutate(channels)
    path: Path = _write_channels(tmp_path, channels)
    with pytest.raises(ConfigError) as raised:
        load_channels(path)
    assert raised.value.key_path == key_path
    assert str(path) in str(raised.value)


def test_facebook_error_names_stage_6(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["platform"] = "facebook"
    with pytest.raises(ConfigError, match="этапе 6"):
        load_channels(_write_channels(tmp_path, channels))


def test_missing_channels_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as raised:
        load_channels(tmp_path / "channels.yaml")
    assert raised.value.key_path == msg.CONFIG_ROOT_KEY


@pytest.mark.parametrize("text", ["- a\n- b\n", "keep_days: [\n", ""], ids=["root_list", "broken_yaml", "empty_file"])
def test_unreadable_root_is_rejected(tmp_path: Path, text: str) -> None:
    path: Path = tmp_path / "planer.yaml"
    path.write_text(text, encoding="utf-8")
    channels: Path = _write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS))
    with pytest.raises(ConfigError) as raised:
        load_planer_config(path, channels)
    assert raised.value.key_path == msg.CONFIG_ROOT_KEY


def test_ensure_configs_exist_copies_both_examples_once(
    planer_paths: PlanerPaths,
    repo_config_example: Path,
    repo_channels_example: Path,
) -> None:
    planer_paths.config_example.write_bytes(repo_config_example.read_bytes())
    planer_paths.channels_example.write_bytes(repo_channels_example.read_bytes())
    created: tuple[Path, ...] = ensure_configs_exist(planer_paths)
    assert set(created) == {planer_paths.config_file, planer_paths.channels_file}
    assert planer_paths.config_file.read_bytes() == repo_config_example.read_bytes()
    assert planer_paths.channels_file.read_bytes() == repo_channels_example.read_bytes()
    assert ensure_configs_exist(planer_paths) == ()


def test_ensure_configs_exist_without_example_fails(planer_paths: PlanerPaths) -> None:
    with pytest.raises(ConfigError):
        ensure_configs_exist(planer_paths)
