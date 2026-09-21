from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import (
    ACCOUNT_NAME_MAX_CHARS,
    ConfigError,
    ConfigProblem,
    PlanerConfig,
    PlanerSettings,
    Privacy,
    load_channels,
    load_planer_config,
    load_settings,
    render_channels_file,
    save_channels_file,
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
    "youtube_pause_seconds": 2,
}
BASE_CHANNELS: dict[str, Any] = {
    "channels": [
        {
            "platform": "youtube",
            "account_name": "Канал UA",
            "handle": "@KanalUA",
            "google_account": "ua@gmail.com",
            "languages": ["uk"],
            "privacy": "public",
        },
        {
            "platform": "youtube",
            "account_name": "Канал RU",
            "handle": "@KanalRU",
            "google_account": "ru@gmail.com",
            "languages": ["ru", "en"],
            "privacy": "unlisted",
        },
    ],
}

# secrets\\channels.json после задачи 5l: шесть полей, канал — две строки (render_channels_file)
OWNER_CHANNELS_FILE: str = """{
  "channels": [
    {"platform": "youtube", "account_name": "Osvald.X", "handle": "@Osvald.X", "google_account": "trustviorel@gmail.com",
     "languages": ["ru"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Oktavian.X", "handle": "@Oktavian.X", "google_account": "oktavian.tibery@gmail.com",
     "languages": ["uk"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Maria Kamenskay", "handle": "@MariaKamenskay", "google_account": "maria.lotos11@gmail.com",
     "languages": ["uk", "ru"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Nick Moss", "handle": "@NickMoss85", "google_account": "nino4kahx@gmail.com",
     "languages": ["en"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Паша Экскаватощик", "handle": "@ПашаЭкскаватощик", "google_account": "warerabeliaev@gmail.com",
     "languages": ["uk"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Українка я", "handle": "@TheImpact-r2b", "google_account": "impactthe95@gmail.com",
     "languages": ["uk"], "privacy": "public"},
    {"platform": "youtube", "account_name": "Українка Я", "handle": "@Ukrainian_girl25", "google_account": "lisathomson2024@gmail.com",
     "languages": ["uk"], "privacy": "public"}
  ]
}
"""


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
    ("youtube_pause_negative", lambda c: c.update(youtube_pause_seconds=-1), "youtube_pause_seconds"),
    ("youtube_pause_negative_fraction", lambda c: c.update(youtube_pause_seconds=-0.5), "youtube_pause_seconds"),
    ("youtube_pause_text", lambda c: c.update(youtube_pause_seconds="2"), "youtube_pause_seconds"),
    ("youtube_pause_fraction_text", lambda c: c.update(youtube_pause_seconds="0.5"), "youtube_pause_seconds"),
    ("youtube_pause_bool", lambda c: c.update(youtube_pause_seconds=True), "youtube_pause_seconds"),
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
    ("account_name_leading_space", lambda c: _channel(0)(c).update(account_name=" Osvald"), "channels[0].account_name"),
    ("account_name_trailing_space", lambda c: _channel(0)(c).update(account_name="Osvald "), "channels[0].account_name"),
    ("account_name_newline", lambda c: _channel(0)(c).update(account_name="Osv\nald"), "channels[0].account_name"),
    ("account_name_tab", lambda c: _channel(0)(c).update(account_name="Osv\tald"), "channels[0].account_name"),
    ("account_name_too_long", lambda c: _channel(0)(c).update(account_name="К" * 101), "channels[0].account_name"),
    ("handle_without_at", lambda c: _channel(0)(c).update(handle="KanalUA"), "channels[0].handle"),
    ("handle_too_short", lambda c: _channel(0)(c).update(handle="@ab"), "channels[0].handle"),
    ("handle_too_long", lambda c: _channel(0)(c).update(handle="@" + "a" * 31), "channels[0].handle"),
    ("handle_space", lambda c: _channel(0)(c).update(handle="@Kanal UA"), "channels[0].handle"),
    ("handle_slash", lambda c: _channel(0)(c).update(handle="@Kanal/UA"), "channels[0].handle"),
    ("handle_blank", lambda c: _channel(0)(c).update(handle=" "), "channels[0].handle"),
    ("handle_not_string", lambda c: _channel(0)(c).update(handle=1), "channels[0].handle"),
    ("handle_duplicate_case", lambda c: _channel(1)(c).update(handle="@kanalua"), "channels[1].handle"),
    ("google_account_blank", lambda c: _channel(0)(c).update(google_account=""), "channels[0].google_account"),
    ("google_account_no_at", lambda c: _channel(0)(c).update(google_account="ua.gmail.com"), "channels[0].google_account"),
    ("google_account_two_at", lambda c: _channel(0)(c).update(google_account="u@a@gmail.com"), "channels[0].google_account"),
    ("google_account_no_local", lambda c: _channel(0)(c).update(google_account="@gmail.com"), "channels[0].google_account"),
    ("google_account_no_domain", lambda c: _channel(0)(c).update(google_account="ua@"), "channels[0].google_account"),
    ("google_account_space", lambda c: _channel(0)(c).update(google_account="u a@gmail.com"), "channels[0].google_account"),
    ("google_account_not_string", lambda c: _channel(0)(c).update(google_account=1), "channels[0].google_account"),
    (
        "handle_duplicate_unicode_form",
        lambda c: (_channel(0)(c).update(handle="@" + COMPOSED[:3]), _channel(1)(c).update(handle="@" + DECOMPOSED[:4])),
        "channels[1].handle",
    ),
    ("languages_empty", lambda c: _channel(0)(c).update(languages=[]), "channels[0].languages"),
    ("languages_uppercase", lambda c: _channel(0)(c).update(languages=["UK"]), "channels[0].languages"),
    ("languages_not_list", lambda c: _channel(0)(c).update(languages="uk"), "channels[0].languages"),
    ("languages_duplicate", lambda c: _channel(1)(c).update(languages=["ru", "ru"]), "channels[1].languages"),
    ("privacy_unknown", lambda c: _channel(0)(c).update(privacy="private"), "channels[0].privacy"),
    ("unknown_top_key", lambda c: c.update(owner="Тест"), "owner"),
    ("unknown_channel_key", lambda c: _channel(0)(c).update(token="x"), "channels[0].token"),
]

MISSING_CHANNEL_FIELDS: list[str] = ["platform", "account_name", "handle", "google_account", "languages", "privacy"]


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
        youtube_pause_seconds=0.5,
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
    assert (channel.account_name, channel.handle, channel.google_account, channel.languages, channel.privacy) == (
        "Название канала на YouTube",
        "@ник_канала",
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
                    "handle": "@xxx",
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
    """«й», собранная из «и» и знака, — то же имя, что «й» одним символом: один токен."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = DECOMPOSED
    config: PlanerConfig = load_planer_config(
        _write_settings(tmp_path, copy.deepcopy(BASE_SETTINGS)),
        _write_channels(tmp_path, channels),
    )
    assert config.channels[0].account_name == COMPOSED


def test_handle_is_normalized_to_nfc_and_keyed_without_case(tmp_path: Path) -> None:
    """Ник в другой форме Unicode — тот же ник и тот же файл токена; --auth находит его с «@» и без."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["handle"] = "@" + DECOMPOSED.replace(" ", "")
    config: PlanerConfig = load_planer_config(
        _write_settings(tmp_path, copy.deepcopy(BASE_SETTINGS)),
        _write_channels(tmp_path, channels),
    )
    wanted: str = "@" + COMPOSED.replace(" ", "")
    assert config.channels[0].handle == wanted
    assert token_file_for(tmp_path, config.channels[0].handle).name == f"{wanted}.token.json"
    assert config.channels[0].key == COMPOSED.replace(" ", "").casefold()
    assert config.channel_by_handle(DECOMPOSED.replace(" ", "").upper()) is config.channels[0]


def test_channel_lookup_by_handle(tmp_path: Path) -> None:
    config: PlanerConfig = load_planer_config(
        _write_settings(tmp_path, copy.deepcopy(BASE_SETTINGS)),
        _write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS)),
    )
    found = config.channel_by_handle("@kanalru")
    assert found is not None and found.languages == ("ru", "en")
    assert config.channel_by_handle("KanalRU") is found
    assert config.channel_by_handle("Канал RU") is None       # по названию каналы не ищутся
    assert config.served_languages == frozenset({"uk", "ru", "en"})


def test_two_ukrainka_channels_differ_by_handle(tmp_path: Path) -> None:
    """«Українка я» и «Українка Я»: названия различаются только регистром — разные ники, разные токены."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0].update(account_name="Українка я", handle="@TheImpact-r2b")
    channels["channels"][1].update(account_name="Українка Я", handle="@Ukrainian_girl25")
    [first, second] = load_channels(_write_channels(tmp_path, channels))
    assert (first.account_name, second.account_name) == ("Українка я", "Українка Я")
    assert token_file_for(tmp_path, first.handle).name.casefold() != token_file_for(tmp_path, second.handle).name.casefold()
    assert token_file_for(tmp_path / "secrets", second.handle) == tmp_path / "secrets" / "@Ukrainian_girl25.token.json"


def test_same_title_with_other_handles_is_allowed(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][1]["account_name"] = "Канал UA"
    [first, second] = load_channels(_write_channels(tmp_path, channels))
    assert first.account_name == second.account_name and first.key != second.key


def test_handle_duplicate_names_both_handles(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][1]["handle"] = "@KANALUA"
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert raised.value.problem == msg.CONFIG_PROBLEM_HANDLE_DUPLICATE.format(value="@KANALUA", other="@KanalUA")


def test_owner_channels_file_renders_back_byte_for_byte(tmp_path: Path) -> None:
    """Файл, который планер пишет при выравнивании, — того же вида, что файл владельца."""
    path: Path = tmp_path / "channels.json"
    path.write_bytes(OWNER_CHANNELS_FILE.encode("utf-8"))
    channels = load_channels(path)
    assert len(channels) == 7
    assert render_channels_file(channels).encode("utf-8") == OWNER_CHANNELS_FILE.encode("utf-8")


def test_save_keeps_previous_file_and_writes_atomically(tmp_path: Path) -> None:
    path: Path = _write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS))
    before: bytes = path.read_bytes()
    channels = load_channels(path)
    save_channels_file(path, tmp_path / "channels.previous.json", channels[:1])
    assert (tmp_path / "channels.previous.json").read_bytes() == before
    assert [channel.handle for channel in load_channels(path)] == ["@KanalUA"]
    assert sorted(item.name for item in tmp_path.iterdir()) == ["channels.json", "channels.previous.json"]


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


def test_zero_youtube_pause_is_accepted(tmp_path: Path) -> None:
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    settings["youtube_pause_seconds"] = 0
    assert load_settings(_write_settings(tmp_path, settings)).youtube_pause_seconds == 0


@pytest.mark.parametrize("value", [0, 0.5, 2])
def test_youtube_pause_may_be_fractional(tmp_path: Path, value: float) -> None:
    """5p: пауза — число не меньше 0, можно дробное; в поставке 0.5."""
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    settings["youtube_pause_seconds"] = value
    loaded: float = load_settings(_write_settings(tmp_path, settings)).youtube_pause_seconds
    assert loaded == value and isinstance(loaded, float)


@pytest.mark.parametrize("value", [-0.5, True, "0.5"])
def test_youtube_pause_rejects_negative_bool_and_text(tmp_path: Path, value: Any) -> None:
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    settings["youtube_pause_seconds"] = value
    with pytest.raises(ConfigError) as raised:
        load_settings(_write_settings(tmp_path, settings))
    assert raised.value.key_path == "youtube_pause_seconds"
    assert msg.CONFIG_PROBLEM_NUMBER_MIN.format(minimum=0.0) in str(raised.value)
    assert "нужно число не меньше 0, можно дробное, например 0.5" in str(raised.value)


def test_missing_youtube_pause_names_the_field(tmp_path: Path) -> None:
    """Умолчания нет: планер, собранный до поля, не должен молча обращаться к YouTube без паузы."""
    settings: dict[str, Any] = copy.deepcopy(BASE_SETTINGS)
    settings.pop("youtube_pause_seconds")
    with pytest.raises(ConfigError, match="youtube_pause_seconds") as raised:
        load_settings(_write_settings(tmp_path, settings))
    assert raised.value.kind is ConfigProblem.FIELD_MISSING


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


def test_account_name_all_is_an_ordinary_title(tmp_path: Path) -> None:
    """--auth ищет канал по нику: название «All» режиму --auth all не мешает."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "ALL"
    assert load_channels(_write_channels(tmp_path, channels))[0].account_name == "ALL"


def test_google_account_is_loaded_as_written(tmp_path: Path) -> None:
    [first, second] = load_channels(_write_channels(tmp_path, copy.deepcopy(BASE_CHANNELS)))
    assert (first.google_account, second.google_account) == ("ua@gmail.com", "ru@gmail.com")


@pytest.mark.parametrize(
    "name",
    ["Новини: Україна", "News | UA", "Канал.", "CON", 'Канал "UA"', "UA/RU?"],
    ids=["colon", "pipe", "trailing_dot", "reserved_windows_name", "quotes", "slash_question"],
)
def test_youtube_title_with_file_name_characters_is_accepted(tmp_path: Path, name: str) -> None:
    """Название — как на YouTube; в именах файлов оно не участвует."""
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = name
    assert load_channels(_write_channels(tmp_path, channels))[0].account_name == name


def test_space_at_edge_names_the_title(tmp_path: Path) -> None:
    channels: dict[str, Any] = copy.deepcopy(BASE_CHANNELS)
    channels["channels"][0]["account_name"] = "Osvald "
    with pytest.raises(ConfigError) as raised:
        load_channels(_write_channels(tmp_path, channels))
    assert raised.value.problem == msg.CONFIG_PROBLEM_ACCOUNT_NAME_SPACE_EDGE.format(value="Osvald ")


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
