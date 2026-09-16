from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig, render_channels_file
from app.google.auth import token_file_for
from app.paths import PlanerPaths
from app.platforms.base import ChannelInfo
from app.platforms.channel_sync import ChannelSync
from app.platforms.fake import FakePlatform
from app.platforms.passport import ENTRY_FIELDS, ChannelPassport
from app.tests.conftest import FIXED_NOW
from app.ui import messages_ru as msg

ConfigFactory = Callable[..., PlanerConfig]


@pytest.fixture
def config(planer_paths: PlanerPaths, repo_planer_config: Path, make_config: ConfigFactory) -> PlanerConfig:
    """Корень как у владельца: planer.json, channels.json и токены обоих каналов."""
    shutil.copyfile(repo_planer_config, planer_paths.config_file)
    built: PlanerConfig = make_config()
    planer_paths.channels_file.write_text(render_channels_file(built.channels), encoding="utf-8")
    for channel in built.channels:
        token_file_for(planer_paths.secrets_dir, channel.handle).write_text("{}", encoding="utf-8")
    return built


def _sync(platform: FakePlatform, paths: PlanerPaths) -> ChannelSync:
    return ChannelSync(platform, paths, FIXED_NOW)


def _passport_json(paths: PlanerPaths) -> dict[str, Any]:
    return json.loads(paths.channels_passport_file.read_text(encoding="utf-8"))


def test_first_check_records_every_field_and_links(
    planer_paths: PlanerPaths, config: PlanerConfig, fake_platform: FakePlatform
) -> None:
    _sync(fake_platform, planer_paths).run(config)
    raw: dict[str, Any] = _passport_json(planer_paths)
    assert [entry["handle"] for entry in raw["channels"]] == ["@yt_ru", "@yt_ua"]     # по названию, затем нику
    entry: dict[str, Any] = raw["channels"][1]
    assert tuple(entry) == ENTRY_FIELDS
    assert entry == {
        "handle": "@yt_ua",
        "account_name": "yt_ua",
        "youtube_channel_id": "UCfakeyt_ua",
        "youtube_title": "yt_ua",
        "youtube_handle_raw": "@yt_ua",
        "google_account": "owner@gmail.com",
        "token_file": "@yt_ua.token.json",
        "channel_url": "https://www.youtube.com/channel/UCfakeyt_ua",
        "handle_url": "https://www.youtube.com/@yt_ua",
        "first_verified_at": "16-03-2027 12:00",
        "last_verified_at": "16-03-2027 12:00",
        "previous_handles": [],
        "previous_titles": [],
    }
    assert planer_paths.channels_passport_file.read_text(encoding="utf-8").endswith("}\n")


def test_repeated_check_keeps_first_time_and_updates_last(
    planer_paths: PlanerPaths, config: PlanerConfig, fake_platform: FakePlatform
) -> None:
    ChannelSync(fake_platform, planer_paths, FIXED_NOW.replace(day=1)).run(config)
    ChannelSync(FakePlatform(), planer_paths, FIXED_NOW).run(config)
    entry: dict[str, Any] = _passport_json(planer_paths)["channels"][1]
    assert (entry["first_verified_at"], entry["last_verified_at"]) == ("01-03-2027 12:00", "16-03-2027 12:00")


@pytest.mark.parametrize(
    "text",
    ["{broken", '["a"]', '{"channels": [{"handle": "@x"}]}', '{"channels": [], "extra": 1}'],
    ids=["broken_json", "not_object", "entry_fields", "extra_key"],
)
def test_unreadable_passport_is_reported_and_rewritten(
    planer_paths: PlanerPaths, config: PlanerConfig, fake_platform: FakePlatform, text: str
) -> None:
    planer_paths.channels_passport_file.write_text(text, encoding="utf-8")
    _config, warnings = _sync(fake_platform, planer_paths).run(config)
    assert len(warnings) == 1
    assert warnings[0].startswith(msg.WARNING_PASSPORT_UNREADABLE.split("{", 1)[0].format())
    assert str(planer_paths.channels_passport_file) in warnings[0]
    assert [entry["handle"] for entry in _passport_json(planer_paths)["channels"]] == ["@yt_ru", "@yt_ua"]


def test_passport_write_failure_is_a_warning_not_a_stop(
    planer_paths: PlanerPaths, config: PlanerConfig, fake_platform: FakePlatform
) -> None:
    planer_paths.channels_passport_file.mkdir()          # на месте файла — папка: запись не пройдёт
    synced, warnings = _sync(fake_platform, planer_paths).run(config)
    assert synced == config
    assert len(warnings) == 1 and str(planer_paths.channels_passport_file) in warnings[0]
    assert warnings[0].startswith(msg.WARNING_PASSPORT_WRITE_FAILED.split("{", 1)[0].format())


def test_entries_of_channels_missing_from_config_are_kept(
    planer_paths: PlanerPaths, config: PlanerConfig, fake_platform: FakePlatform
) -> None:
    _sync(fake_platform, planer_paths).run(config)
    only_ua: PlanerConfig = replace(config, channels=config.channels[:1])
    _sync(FakePlatform(), planer_paths).run(only_ua)
    assert [entry["handle"] for entry in _passport_json(planer_paths)["channels"]] == ["@yt_ru", "@yt_ua"]


def test_record_replaces_entry_of_the_same_channel_id(tmp_path: Path, make_config: ConfigFactory) -> None:
    passport: ChannelPassport = ChannelPassport(tmp_path / "channels_passport.json")
    old: ChannelConfig = make_config().channels[0]
    new: ChannelConfig = replace(old, handle="@yt_ua_new", account_name="Новое")
    info: ChannelInfo = FakePlatform.default_channel_info(old)
    passport.record_verified(old, old, info, token_file="@yt_ua.token.json", verified_at="01-01-2027 10:00")
    entry = passport.record_verified(
        old, new, replace(info, title="Новое"), token_file="@yt_ua_new.token.json", verified_at="02-01-2027 10:00"
    )
    assert passport.entries == (entry,)
    assert (entry.previous_handles, entry.previous_titles, entry.first_verified_at) == (
        ("@yt_ua",), ("yt_ua",), "01-01-2027 10:00"
    )
    assert passport.save() is None
    loaded, problem = ChannelPassport.load(passport.path)
    assert problem is None and loaded.entries == (entry,)
