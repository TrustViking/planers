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
    ensure_config_exists,
    load_planer_config,
)
from app.paths import PlanerPaths
from app.ui import messages_ru as msg

BASE_CONFIG: dict[str, Any] = {
    "owner": "Тест",
    "min_lead_minutes": 60,
    "inbox_keep_days": 14,
    "channels": [
        {"id": "yt_ua", "platform": "youtube", "account_name": "Test UA", "languages": ["uk"]},
        {"id": "yt_ru", "platform": "youtube", "account_name": "Test RU", "languages": ["ru", "en"]},
    ],
}


def _write_config(tmp_path: Path, config: Any) -> Path:
    path: Path = tmp_path / "planer.yaml"
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _channel(index: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda config: config["channels"][index]


INVALID_CASES: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    ("owner_empty", lambda c: c.update(owner=""), "owner"),
    ("owner_missing", lambda c: c.pop("owner"), "owner"),
    ("min_lead_negative", lambda c: c.update(min_lead_minutes=-1), "min_lead_minutes"),
    ("min_lead_text", lambda c: c.update(min_lead_minutes="60"), "min_lead_minutes"),
    ("keep_days_zero", lambda c: c.update(inbox_keep_days=0), "inbox_keep_days"),
    ("channels_empty", lambda c: c.update(channels=[]), "channels"),
    ("channels_missing", lambda c: c.pop("channels"), "channels"),
    ("channel_not_mapping", lambda c: c["channels"].__setitem__(0, "yt_ua"), "channels[0]"),
    ("id_uppercase", lambda c: _channel(0)(c).update(id="YT_UA"), "channels[0].id"),
    ("id_starts_with_digit", lambda c: _channel(0)(c).update(id="1yt"), "channels[0].id"),
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
    ("unknown_top_key", lambda c: c.update(extra=1), "extra"),
    ("unknown_channel_key", lambda c: _channel(0)(c).update(token="x"), "channels[0].token"),
]


def test_repo_example_loads(repo_config_example: Path) -> None:
    config: PlanerConfig = load_planer_config(repo_config_example)
    assert config.owner == "Иван"
    assert (config.min_lead_minutes, config.inbox_keep_days) == (60, 14)
    assert [channel.id for channel in config.channels] == ["yt_ua", "yt_ru"]
    assert config.channels[0].account_name == "Іван UA"
    assert config.channels[1].languages == ("ru", "en")
    assert config.channels[1].privacy is Privacy.PUBLIC
    assert config.channels[1].auto_start is True
    assert config.channels[1].set_thumbnail is True


def test_optional_keys_take_defaults(tmp_path: Path) -> None:
    minimal: dict[str, Any] = {
        "owner": "Тест",
        "channels": [{"id": "yt_ua", "platform": "youtube", "account_name": "Test UA", "languages": ["uk"]}],
    }
    config: PlanerConfig = load_planer_config(_write_config(tmp_path, minimal))
    assert (config.min_lead_minutes, config.inbox_keep_days) == (60, 14)
    assert config.channels[0].privacy is Privacy.PUBLIC
    assert config.served_languages == frozenset({"uk"})


@pytest.mark.parametrize(("mutate", "key_path"), [case[1:] for case in INVALID_CASES], ids=[case[0] for case in INVALID_CASES])
def test_invalid_config_is_rejected_with_key_path(
    tmp_path: Path,
    mutate: Callable[[dict[str, Any]], object],
    key_path: str,
) -> None:
    config: dict[str, Any] = copy.deepcopy(BASE_CONFIG)
    mutate(config)
    path: Path = _write_config(tmp_path, config)
    with pytest.raises(ConfigError) as raised:
        load_planer_config(path)
    assert raised.value.key_path == key_path
    assert str(path) in str(raised.value)


def test_facebook_error_names_stage_6(tmp_path: Path) -> None:
    config: dict[str, Any] = copy.deepcopy(BASE_CONFIG)
    config["channels"][0]["platform"] = "facebook"
    with pytest.raises(ConfigError, match="этапе 6"):
        load_planer_config(_write_config(tmp_path, config))


@pytest.mark.parametrize("text", ["- a\n- b\n", "owner: [\n", ""], ids=["root_list", "broken_yaml", "empty_file"])
def test_unreadable_root_is_rejected(tmp_path: Path, text: str) -> None:
    path: Path = tmp_path / "planer.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_planer_config(path)
    assert raised.value.key_path == msg.CONFIG_ROOT_KEY


def test_ensure_config_exists_copies_example_once(planer_paths: PlanerPaths, repo_config_example: Path) -> None:
    planer_paths.config_example.write_bytes(repo_config_example.read_bytes())
    assert ensure_config_exists(planer_paths) is False
    assert planer_paths.config_file.read_bytes() == repo_config_example.read_bytes()
    assert ensure_config_exists(planer_paths) is True


def test_ensure_config_exists_without_example_fails(planer_paths: PlanerPaths) -> None:
    with pytest.raises(ConfigError):
        ensure_config_exists(planer_paths)
