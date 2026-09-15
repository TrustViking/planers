from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import (
    ACCOUNT_NAME_MAX_CHARS,
    AUTH_ALL,
    ConfigError,
    ConfigProblem,
    PlanerConfig,
    PlanerSettings,
    Privacy,
    load_channels,
    load_planer_config,
    load_settings,
)
from app.google.auth import token_file_for
from app.ui import messages_ru as msg

COMPOSED: str = "Мій канал"                        # «й» — один символ U+0439
DECOMPOSED: str = "Мій канал"           # «и» U+0438 + знак краткой U+0306

BASE_SETTINGS: dict[str, Any] = {
    "min_lead_minutes": 60,
    "keep_days": 30,
    "auto_start": True,
    "set_thumbnail": True,
    "category_id": "22",
}
BASE_CHANNELS: dict[str, Any] = {
    "channels": [
        {
            "platform": "youtube",
            "account_name": "Канал UA",
            "google_account": "ua@gmail.com",
            "languages": ["uk"],
            "privacy": "public",
        },
        {
            "platform": "youtube",
            "account_name": "Канал RU",
            "google_account": "ru@gmail.com",
            "languages": ["ru", "en"],
            "privacy": "unlisted",
        },
    ],
}


def _write(path: Path, config: Any) -> Path:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_settings(tmp_path: Path, config: Any) -> Path:
    return _write(tmp_path / "planer.json", config)


def _write_channels(tmp_path: Path, config: Any) -> Path:
    return _write(tmp_path / "channels.json", config)


def _channel(index: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda config: config["channels"][index]


INVALID_SETTINGS: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    ("unknown_key_rejected", lambda c: c.update(operator="Тест"), "operator"),
    ("min_lead_negative", lambda c: c.update(min_lead_minutes=-1), "min_lead_minutes"),
    ("min_lead_text", lambda c: c.update(min_lead_minutes="60"), "min_lead_minutes"),
    ("keep_days_zero", lambda c: c.update(keep_days=0), "keep_days"),
    ("auto_start_not_bool", lambda c: c.update(auto_start="yes"), "auto_start"),
    ("set_thumbnail_not_bool", lambda c: c.update(set_thumbnail=1), "set_thumbnail"),
    ("category_not_string", lambda c: c.update(category_id=22), "category_id"),
    ("category_blank", lambda c: c.update(category_id=" "), "category_id"),
    ("channels_in_settings", lambda c: c.update(channels=[]), "channels"),
]

MISSING_SETTINGS: list[str] = list(BASE_SETTINGS)

INVALID_CHANNELS: list[tuple[str, Callable[[dict[str, Any]], object], str]] = [
    ("channels_empty", lambda c: c.update(channels=[]), "channels"),
    ("channel_not_mapping", lambda c: c["channels"].__setitem__(0, "Канал UA"), "channels[0]"),
    ("id_removed", lambda c: _channel(0)(c).update(id="yt_ua"), "channels[0].id"),
    ("auto_start_moved_to_planer", lambda c: _channel(0)(c).update(auto_start=True), "channels[0].auto_start"),
    ("category_moved_to_planer", lambda c: _channel(0)(c).update(category_id="22"), "channels[0].category_id"),
    ("platform_facebook", lambda c: _channel(0)(c).update(platform="facebook"), "channels[0].platform"),
    ("platform_unknown", lambda c: _channel(0)(c).update(platform="rumble"), "channels[0].platform"),
    ("account_name_blank", lambda c: _channel(0)(c).update(account_name="  "), "channels[0].account_name"),
    ("account_name_slash", lambda c: _channel(0)(c).update(account_name="UA/RU"), "channels[0].account_name"),
    ("account_name_colon", lambda c: _channel(0)(c).update(account_name="UA: live"), "channels[0].account_name"),
    ("account_name_quote", lambda c: _channel(0)(c).update(account_name='Канал "UA"'), "channels[0].account_name"),
    ("account_name_trailing_dot", lambda c: _channel(0)(c).update(account_name="Osvald."), "channels[0].account_name"),
    ("account_name_leading_space", lambda c: _channel(0)(c).update(account_name=" Osvald"), "channels[0].account_name"),
    ("account_name_newline", lambda c: _channel(0)(c).update(account_name="Osv\nald"), "channels[0].account_name"),
    ("account_name_reserved", lambda c: _channel(0)(c).update(account_name="con"), "channels[0].account_name"),
    ("account_name_duplicate", lambda c: _channel(1)(c).update(account_name="Канал UA"), "channels[1].account_name"),
    ("account_name_duplicate_case", lambda c: _channel(1)(c).update(account_name="канал ua"), "channels[1].account_name"),
    ("account_name_too_long", lambda c: _channel(0)(c).update(account_name="К" * 101), "channels[0].account_name"),
    ("account_name_auth_all", lambda c: _channel(0)(c).update(account_name="All"), "channels[0].account_name"),
    ("google_account_blank", lambda c: _channel(0)(c).update(google_account=""), "channels[0].google_account"),
    ("google_account_no_at", lambda c: _channel(0)(c).update(google_account="ua.gmail.com"), "channels[0].google_account"),
    ("google_account_two_at", lambda c: _channel(0)(c).update(google_account="u@a@gmail.com"), "channels[0].google_account"),
    ("google_account_no_local", lambda c: _channel(0)(c).update(google_account="@gmail.com"), "channels[0].google_account"),
    ("google_account_no_domain", lambda c: _channel(0)(c).update(google_account="ua@"), "channels[0].google_account"),
    ("google_account_space", lambda c: _channel(0)(c).update(google_account="u a@gmail.com"), "channels[0].google_account"),
    ("google_account_not_string", lambda c: _channel(0)(c).update(google_account=1), "channels[0].google_account"),
    (
        "account_name_duplicate_unicode_form",
        lambda c: (_channel(0)(c).update(account_name=COMPOSED), _channel(1)(c).update(account_name=DECOMPOSED)),
        "channels[1].account_name",
    ),
    ("languages_empty", lambda c: _channel(0)(c).update(languages=[]), "channels[0].languages"),
    ("languages_uppercase", lambda c: _channel(0)(c).update(languages=["UK"]), "channels[0].languages"),
    ("languages_not_list", lambda c: _channel(0)(c).update(languages="uk"), "channels[0].languages"),
    ("languages_duplicate", lambda c: _channel(1)(c).update(languages=["ru", "ru"]), "channels[1].languages"),
    ("privacy_unknown", lambda c: _channel(0)(c).update(privacy="private"), "channels[0].privacy"),
    ("unknown_top_key", lambda c: c.update(owner="Тест"), "owner"),
    ("unknown_channel_key", lambda c: _channel(0)(c).update(token="x"), "channels[0].token"),
]

MISSING_CHANNEL_FIELDS: list[str] = ["platform", "account_name", "google_account", "languages", "privacy"]


def test_repo_planer_json_and_channels_example_load_together(
    repo_planer_config: Path,
    repo_channels_example: Path,
) -> None:
    config: PlanerConfig = load_planer_config(repo_planer_config, repo_channels_example)
    assert config.settings == PlanerSettings(
        min_lead_minutes=60,
        keep_days=30,
        auto_start=True,
        set_thumbnail=True,
        category_id="22",
    )
    assert [channel.account_name for channel in config.channels] == ["Канал UA", "Канал RU"]
    assert config.channels[1].languages == ("ru", "en")
    assert config.channels[1].privacy is Privacy.UNLISTED


def test_console_templates_are_valid_configs(tmp_path: Path, repo_planer_config: Path) -> None:
    """Шаблоны, которые печатает main, — рабочие файлы; шаблон planer.json совпадает с файлом репо."""
    settings: Path = tmp_path / "planer.json"
    settings.write_text(msg.CONFIG_PLANER_TEMPLATE, encoding="utf-8")
    channels: Path = tmp_path / "channels.json"
    channels.write_text(msg.CONFIG_CHANNELS_TEMPLATE, encoding="utf-8")
    config: PlanerConfig = load_planer_config(settings, channels)
    assert config.settings == load_settings(repo_planer_config)
    [channel] = config.channels
    assert (channel.account_name, channel.google_account, channel.languages, channel.privacy) == (
        "Osvald.X",
        "you@gmail.com",
        ("ru",),
        Privacy.UNLISTED,
    )


def test_unknown_language_code_is_accepted(tmp_path: Path) -> None:
    """Язык назначает оператор: справочника кодов у планера нет."""
    channels: Path = _write_channels(
        tmp_path,
        {
            "channels": [
                {
                    "platform": "youtube",
                    "account_name": "X",
                    "google_account": "x@gmail.com",
                    "languages": ["zz"],
                    "privacy": "public",
                },
            ],
        },
    )
    assert load_channels(channels)[0].languages == ("zz",)


def test_account_name_with_dot_inside_is_accepted(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "Osvald.X"
    assert load_channels(_write_channels(tmp_path, channels))[0].account_name == "Osvald.X"


def test_account_name_of_max_length_is_accepted(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "К" * ACCOUNT_NAME_MAX_CHARS
    assert len(load_channels(_write_channels(tmp_path, channels))[0].account_name) == ACCOUNT_NAME_MAX_CHARS


def test_too_long_account_name_names_the_limit_and_length(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "К" * 120
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert raised.value.problem == msg.CONFIG_PROBLEM_ACCOUNT_NAME_TOO_LONG.format(maximum=100, length=120)


def test_account_name_is_normalized_to_nfc(tmp_path: Path) -> None:
    """«й», собранная из «и» и знака, — то же имя, что «й» одним символом: один токен, одна привязка."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = DECOMPOSED
    config: PlanerConfig = load_planer_config(
        _write_settings(tmp_path, copy.deepcopy(BASE_SETTINGS)),
        _write_channels(tmp_path, channels),
    )
    assert config.channels[0].account_name == COMPOSED
    assert token_file_for(tmp_path, config.channels[0].account_name).name == f"{COMPOSED}.token.json"
    assert config.channel(DECOMPOSED) is config.channels[0]      # --auth с именем в другой форме


def test_channel_lookup_by_account_name(tmp_path: Path) -> None:
    config: PlanerConfig = load_planer_config(
        _write_settings(tmp_path, copy.deepcopy(BASE_SETTINGS)),
        _write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS)),
    )
    found = config.channel("Канал RU")
    assert found is not None and found.languages == ("ru", "en")
    assert config.channel("Канал XX") is None
    assert config.served_languages == frozenset({"uk", "ru", "en"})


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
    with pytest.raises(ConfigError) as raised:
        load_settings(path)
    assert raised.value.key_path == key_path
    assert raised.value.kind is ConfigProblem.INVALID
    assert str(path) in str(raised.value)


@pytest.mark.parametrize("field", MISSING_SETTINGS)
def test_every_settings_field_is_required(tmp_path: Path, field: str) -> None:
    """Умолчаний в коде нет: пропущенное поле — ошибка, для которой main печатает шаблон."""
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    settings.pop(field)
    with pytest.raises(ConfigError) as raised:
        load_settings(_write_settings(tmp_path, settings))
    assert raised.value.key_path == field
    assert raised.value.kind is ConfigProblem.FIELD_MISSING
    assert raised.value.is_template_needed


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


@pytest.mark.parametrize("field", MISSING_CHANNEL_FIELDS)
def test_every_channel_field_is_required(tmp_path: Path, field: str) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0].pop(field)
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert raised.value.key_path == f"channels[0].{field}"
    assert raised.value.kind is ConfigProblem.FIELD_MISSING


def test_account_name_all_is_taken_by_auth_all(tmp_path: Path) -> None:
    """Канал «all» перехватил бы режим --auth all: имя занято без учёта регистра."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "ALL"
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert raised.value.problem == msg.CONFIG_PROBLEM_ACCOUNT_NAME_AUTH_ALL.format(value="ALL", auth_all=AUTH_ALL)


def test_google_account_is_loaded_as_written(tmp_path: Path) -> None:
    [first, second] = load_channels(_write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS)))
    assert (first.google_account, second.google_account) == ("ua@gmail.com", "ru@gmail.com")


def test_account_name_problem_names_the_character(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "UA?"
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert "«?»" in raised.value.problem


def test_facebook_error_names_stage_6(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["platform"] = "facebook"
    with pytest.raises(ConfigError, match="этапе 6"):
        load_channels(_write_channels(tmp_path, channels))


def test_duplicate_field_in_json_object_is_rejected(tmp_path: Path) -> None:
    path: Path = tmp_path / "planer.json"
    path.write_text(
        '{"min_lead_minutes": 60, "min_lead_minutes": 10, "keep_days": 30,'
        ' "auto_start": true, "set_thumbnail": true, "category_id": "22"}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as raised:
        load_settings(path)
    assert raised.value.key_path == "min_lead_minutes"
    assert raised.value.problem == msg.CONFIG_PROBLEM_DUPLICATE_KEY


@pytest.mark.parametrize("loader", [load_channels, load_settings], ids=["channels", "settings"])
def test_missing_file_is_reported_and_needs_template(tmp_path: Path, loader: Callable[[Path], object]) -> None:
    with pytest.raises(ConfigError) as raised:
        loader(tmp_path / "absent.json")
    assert raised.value.key_path == msg.CONFIG_ROOT_KEY
    assert raised.value.kind is ConfigProblem.FILE_MISSING
    assert raised.value.is_template_needed
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize(
    "text",
    ['["a", "b"]', '{"keep_days": [', "", "keep_days: 30\n"],
    ids=["root_list", "broken_json", "empty_file", "key_value_syntax"],
)
def test_unreadable_root_is_rejected(tmp_path: Path, text: str) -> None:
    path: Path = tmp_path / "planer.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_settings(path)
    assert raised.value.key_path == msg.CONFIG_ROOT_KEY
    assert not raised.value.is_template_needed
