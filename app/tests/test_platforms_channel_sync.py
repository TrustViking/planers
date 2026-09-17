from __future__ import annotations

import random
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig, load_planer_config, render_channels_file
from app.google.auth import token_file_for
from app.output.console import render_console
from app.output.report import OutcomeKind
from app.paths import PlanerPaths
from app.pipeline.runner import RunMode, RunOutcome, run
from app.platforms.base import ChannelInfo, PlatformError
from app.platforms.channel_sync import ChannelSync
from app.platforms.fake import FakePlatform
from app.platforms.passport import ChannelPassport, PassportEntry
from app.platforms.channel import Channel, ChannelBook, ChannelStatus
from app.platforms.verified import VerifiedPlatform
from app.tests.conftest import FIXED_NOW, FakeFormSender
from app.ui import messages_ru as msg

ConfigFactory = Callable[..., PlanerConfig]
OLD: str = "@yt_ua"
NEW: str = "@yt_ua_new"
CHANNEL_ID: str = "UCyt_ua"


@pytest.fixture
def owner_root(planer_paths: PlanerPaths, repo_planer_config: Path) -> PlanerPaths:
    shutil.copyfile(repo_planer_config, planer_paths.config_file)
    return planer_paths


def _channel(make_config: ConfigFactory, handle: str = OLD, account_name: str = "Канал UA") -> ChannelConfig:
    return replace(make_config().channels[0], handle=handle, account_name=account_name)


def _write_config(paths: PlanerPaths, *channels: ChannelConfig) -> PlanerConfig:
    paths.channels_file.write_text(render_channels_file(channels), encoding="utf-8")
    return load_planer_config(paths.config_file, paths.channels_file)


def _token(paths: PlanerPaths, handle: str, content: str = "token") -> Path:
    path: Path = token_file_for(paths.secrets_dir, handle)
    path.write_text(content, encoding="utf-8")
    return path


def _answer(platform: FakePlatform, key: str, channel: ChannelConfig, **changes: Any) -> ChannelInfo:
    """Что YouTube отвечает на токен канала с ключом key: тот же id CHANNEL_ID, поля — из changes."""
    info: ChannelInfo = replace(
        FakePlatform.default_channel_info(channel), youtube_channel_id=CHANNEL_ID, **changes
    )
    platform.channel_info[key] = info
    return info


def _seed_passport(paths: PlanerPaths, channel: ChannelConfig) -> None:
    passport: ChannelPassport = ChannelPassport(paths.channels_passport_file)
    info: ChannelInfo = replace(FakePlatform.default_channel_info(channel), youtube_channel_id=CHANNEL_ID)
    passport.record_verified(
        channel, channel, info,
        token_file=token_file_for(paths.secrets_dir, channel.handle).name, verified_at="01-09-2026 10:00",
    )
    assert passport.save() is None


def _sync(platform: FakePlatform, paths: PlanerPaths) -> ChannelSync:
    return ChannelSync(platform, paths, FIXED_NOW)


def _statuses(sync: ChannelSync) -> dict[str, ChannelStatus]:
    return {channel.key: channel.status for channel in sync.take_channels()}


def _entry(paths: PlanerPaths, key: str) -> PassportEntry | None:
    return ChannelPassport.load(paths.channels_passport_file)[0].find_by_key(key)


def _aligned(title_before: str, handle_before: str, title_after: str, handle_after: str) -> str:
    return msg.WARNING_CHANNEL_ALIGNED.format(
        youtube_channel_id=CHANNEL_ID, title_before=title_before, handle_before=handle_before,
        title_after=title_after, handle_after=handle_after,
    )


def test_title_changed_on_youtube_is_aligned(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    before: bytes = owner_root.channels_file.read_bytes()
    token: Path = _token(owner_root, OLD)
    _answer(fake_platform, "yt_ua", channel, title="Новое название")
    synced, warnings = _sync(fake_platform, owner_root).run(config)
    assert [item.account_name for item in synced.channels] == ["Новое название"]
    assert [item.account_name for item in load_planer_config(owner_root.config_file, owner_root.channels_file).channels] == [
        "Новое название"
    ]
    assert owner_root.channels_previous_file.read_bytes() == before
    assert token.read_text(encoding="utf-8") == "token"
    entry: PassportEntry | None = _entry(owner_root, "yt_ua")
    assert entry is not None and entry.account_name == "Новое название" and entry.previous_titles == ("Канал UA",)
    assert warnings == [_aligned("Канал UA", OLD, "Новое название", OLD)]
    assert fake_platform.logins == [] and fake_platform.describe_without_login == ["yt_ua"]


def test_handle_changed_on_youtube_is_aligned_by_passport(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    _seed_passport(owner_root, channel)
    _token(owner_root, OLD)
    _answer(fake_platform, "yt_ua", channel, handle_raw="@YT_UA_new")
    sync: ChannelSync = _sync(fake_platform, owner_root)
    synced, warnings = sync.run(config)
    assert [item.handle for item in synced.channels] == ["@YT_UA_new"]     # написание — как прислал YouTube
    [channel] = sync.take_channels()
    assert (channel.key, channel.status, channel.config) == ("yt_ua_new", ChannelStatus.READY, synced.channels[0])
    assert channel.token_file == token_file_for(owner_root.secrets_dir, "@YT_UA_new")
    assert not token_file_for(owner_root.secrets_dir, OLD).exists()
    assert token_file_for(owner_root.secrets_dir, "@YT_UA_new").read_text(encoding="utf-8") == "token"
    assert _entry(owner_root, "yt_ua") is None
    entry: PassportEntry | None = _entry(owner_root, "yt_ua_new")
    assert entry is not None
    assert (entry.previous_handles, entry.token_file, entry.first_verified_at) == (
        (OLD,), "@YT_UA_new.token.json", "01-09-2026 10:00"
    )
    assert warnings == [_aligned("Канал UA", OLD, "Канал UA", "@YT_UA_new")]
    assert fake_platform.logins == []


def test_foreign_token_is_dropped_at_start(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    """Токен ведёт на канал с другим ником, паспорт этого не подтверждает: файл удалён, NEEDS_LOGIN."""
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    before: bytes = owner_root.channels_file.read_bytes()
    token: Path = _token(owner_root, OLD)
    _answer(fake_platform, "yt_ua", channel, handle_raw="@lisathomson-v3l", title="Lisa Thomson")
    sync: ChannelSync = _sync(fake_platform, owner_root)
    synced, warnings = sync.run(config)
    assert synced == config and owner_root.channels_file.read_bytes() == before
    assert not token.exists()
    assert warnings == [
        msg.WARNING_TOKEN_REJECTED.format(
            account_name="Канал UA", handle=OLD, youtube_title="Lisa Thomson",
            youtube_handle="@lisathomson-v3l", youtube_channel_id=CHANNEL_ID,
        )
    ]
    assert _statuses(sync) == {"yt_ua": ChannelStatus.NEEDS_LOGIN}
    assert fake_platform.dropped_logins == ["yt_ua"] and fake_platform.logins == []


def test_same_handle_with_other_id_in_passport_drops_the_token(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    _seed_passport(owner_root, channel)
    token: Path = _token(owner_root, OLD)
    fake_platform.channel_info["yt_ua"] = replace(FakePlatform.default_channel_info(channel), youtube_channel_id="UCother")
    sync: ChannelSync = _sync(fake_platform, owner_root)
    sync.run(config)
    assert not token.exists()
    assert _statuses(sync) == {"yt_ua": ChannelStatus.NEEDS_LOGIN}


def test_channel_without_handle_confirmed_by_passport_is_refused_and_token_kept(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    """Паспорт подтверждает id, но ника на YouTube нет: токен свой — не удаляется, канал — отказ."""
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    _seed_passport(owner_root, channel)
    token: Path = _token(owner_root, OLD)
    _answer(fake_platform, "yt_ua", channel, handle_raw=None)
    sync: ChannelSync = _sync(fake_platform, owner_root)
    _, warnings = sync.run(config)
    assert token.exists() and warnings == []
    [refused] = sync.take_channels()
    assert refused.status is ChannelStatus.REFUSED
    assert refused.error is not None and refused.error.code == "channelHandleMissing"
    assert "planer.bat --auth" not in refused.error.message


def test_handle_fixed_by_hand_finds_token_under_old_handle(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    old_channel: ChannelConfig = _channel(make_config)
    _seed_passport(owner_root, old_channel)
    _token(owner_root, OLD)
    new_channel: ChannelConfig = _channel(make_config, NEW)
    config: PlanerConfig = _write_config(owner_root, new_channel)
    _answer(fake_platform, "yt_ua", old_channel, handle_raw=NEW)       # токен старого файла ведёт на канал с новым ником
    sync: ChannelSync = _sync(fake_platform, owner_root)
    synced, warnings = sync.run(config)
    assert synced.channels == config.channels
    assert _statuses(sync) == {"yt_ua_new": ChannelStatus.READY}
    assert not token_file_for(owner_root.secrets_dir, OLD).exists()
    assert token_file_for(owner_root.secrets_dir, NEW).read_text(encoding="utf-8") == "token"
    entry: PassportEntry | None = _entry(owner_root, "yt_ua_new")
    assert entry is not None and entry.previous_handles == (OLD,) and _entry(owner_root, "yt_ua") is None
    assert warnings == [_aligned("Канал UA", NEW, "Канал UA", NEW)]
    assert fake_platform.logins == [] and fake_platform.describe_without_login == ["yt_ua"]


def test_existing_target_token_blocks_the_rename(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    before: bytes = owner_root.channels_file.read_bytes()
    _seed_passport(owner_root, channel)
    _token(owner_root, OLD, "old")
    _token(owner_root, NEW, "other")
    _answer(fake_platform, "yt_ua", channel, handle_raw=NEW)
    synced, warnings = _sync(fake_platform, owner_root).run(config)
    assert synced == config and owner_root.channels_file.read_bytes() == before
    assert token_file_for(owner_root.secrets_dir, OLD).read_text(encoding="utf-8") == "old"
    assert token_file_for(owner_root.secrets_dir, NEW).read_text(encoding="utf-8") == "other"
    assert warnings == [
        msg.WARNING_TOKEN_RENAME_SKIPPED.format(
            account_name="Канал UA", handle=OLD, youtube_channel_id=CHANNEL_ID, handle_after=NEW,
            target=token_file_for(owner_root.secrets_dir, NEW),
        )
    ]
    entry: PassportEntry | None = _entry(owner_root, "yt_ua")
    assert entry is not None and entry.handle == OLD


def test_channel_without_token_is_not_asked_at_all(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = _write_config(owner_root, *make_config().channels)
    fake_platform.tokens_missing = {"yt_ua", "yt_ru"}
    sync: ChannelSync = _sync(fake_platform, owner_root)
    synced, warnings = sync.run(config)
    assert synced == config and warnings == []
    assert _statuses(sync) == {"yt_ua": ChannelStatus.NEEDS_LOGIN, "yt_ru": ChannelStatus.NEEDS_LOGIN}
    assert fake_platform.describe_calls == [] and fake_platform.logins == []
    assert not owner_root.channels_passport_file.exists()


def test_describe_failure_skips_the_channel(
    owner_root: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = _write_config(owner_root, *make_config().channels)
    for channel in config.channels:
        _token(owner_root, channel.handle)
    fake_platform.fail_describe["yt_ua"] = PlatformError("backendError", "503")
    fake_platform.tokens_missing = {"yt_ru"}                          # токен отозван: нужен вход, но не сейчас
    sync: ChannelSync = _sync(fake_platform, owner_root)
    synced, warnings = sync.run(config)
    assert synced == config and warnings == []
    assert fake_platform.logins == []
    assert fake_platform.describe_without_login == ["yt_ua", "yt_ru"]
    channels: dict[str, Channel] = {channel.key: channel for channel in sync.take_channels()}
    assert channels["yt_ua"].status is ChannelStatus.FAILED
    assert channels["yt_ua"].error is not None and channels["yt_ua"].error.code == "backendError"
    assert channels["yt_ru"].status is ChannelStatus.NEEDS_LOGIN


def test_output_and_form_use_values_aligned_at_start(
    owner_root: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    now: datetime,
    rng: random.Random,
) -> None:
    channel: ChannelConfig = _channel(make_config)
    config: PlanerConfig = _write_config(owner_root, channel)
    _seed_passport(owner_root, channel)
    _token(owner_root, OLD)
    _answer(fake_platform, "yt_ua", channel, handle_raw=NEW, title="Новое название")
    _answer(fake_platform, "yt_ua_new", _channel(make_config, NEW, "Новое название"))
    book: ChannelBook = ChannelBook(fake_platform, _sync(fake_platform, owner_root))
    synced, warnings = book.check_without_login(config)
    make_package(owner_root.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    form_sender: FakeFormSender = FakeFormSender()
    outcome: RunOutcome = run(
        RunMode.FULL, synced, owner_root, VerifiedPlatform(fake_platform, book), form_sender, now, rng,
        channel_warnings=warnings, logins=book,
    )
    assert outcome.report is not None
    assert [call.account_name for call in form_sender.calls] == ["Новое название"]
    [created] = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.CREATED]
    assert (created.account_name, created.handle) == ("Новое название", NEW)
    assert outcome.report.run_warnings[0] == _aligned("Канал UA", OLD, "Новое название", NEW)
    assert "17-03-2027 19:00  uk  Новое название @yt_ua_new" in owner_root.keys_file.read_text(encoding="utf-8")
    console: str = render_console(outcome.report, root=owner_root.root, channel_order=("yt_ua_new",))
    assert "  Новое название @yt_ua_new (owner@gmail.com)" in console.splitlines()
    assert f"  {_aligned('Канал UA', OLD, 'Новое название', NEW)}" in console.splitlines()
    assert [call.channel_id for call in fake_platform.created] == ["yt_ua_new"]
