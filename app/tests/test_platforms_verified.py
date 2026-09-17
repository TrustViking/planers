from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig, render_channels_file
from app.output.report import OutcomeKind
from app.paths import PlanerPaths
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, run
from app.platforms.base import LOGIN_REQUIRED_CODE, ChannelInfo, PlatformError, PlatformNoticeKind
from app.platforms.channel import ERROR_CHANNEL_HANDLE_MISMATCH, ChannelBindingError, ChannelBook, ChannelStatus
from app.platforms.channel_sync import ChannelSync
from app.platforms.fake import FakePlatform
from app.platforms.verified import VerifiedPlatform
from app.tests.conftest import FIXED_NOW, FakeFormSender

ConfigFactory = Callable[..., PlanerConfig]


def _started(platform: FakePlatform, paths: PlanerPaths, config: PlanerConfig) -> tuple[VerifiedPlatform, ChannelBook]:
    """Как в main: проверка каналов без браузера, затем шлюз над площадкой."""
    platform.secrets_dir = paths.secrets_dir
    paths.channels_file.write_text(render_channels_file(config.channels), encoding="utf-8")
    for channel in config.channels:
        (paths.secrets_dir / f"{channel.handle}.token.json").write_text("t", encoding="utf-8")
    book: ChannelBook = ChannelBook(platform, ChannelSync(platform, paths, FIXED_NOW))
    book.check_without_login(config)
    return VerifiedPlatform(platform, book), book


def test_ready_channel_goes_through_without_second_describe(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    platform, _ = _started(fake_platform, planer_paths, config)
    channel: ChannelConfig = config.channels[0]
    platform.list_upcoming(channel)
    assert platform.describe_channel(channel) == FakePlatform.default_channel_info(channel)
    assert fake_platform.describe_calls == ["yt_ua", "yt_ru"]       # только проверка при старте
    assert fake_platform.list_calls == ["yt_ua"]


def test_not_ready_channel_never_reaches_the_platform(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    platform: VerifiedPlatform = VerifiedPlatform(
        fake_platform, ChannelBook(fake_platform, ChannelSync(fake_platform, planer_paths, FIXED_NOW))
    )
    with pytest.raises(PlatformError) as raised:
        platform.create_broadcast(config.channels[0], None)  # type: ignore[arg-type]
    assert raised.value.code == LOGIN_REQUIRED_CODE
    assert fake_platform.created == [] and fake_platform.describe_calls == []


def test_refused_channel_raises_its_stored_error(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    platform, book = _started(fake_platform, planer_paths, config)
    channel: ChannelConfig = config.channels[0]
    error: ChannelBindingError = ChannelBindingError(ERROR_CHANNEL_HANDLE_MISMATCH, "не тот канал")
    book.channel(channel).mark_refused(FakePlatform.default_channel_info(channel), error)
    for _ in range(2):
        with pytest.raises(ChannelBindingError) as raised:
            platform.list_upcoming(channel)
        assert raised.value is error
    assert fake_platform.list_calls == []


def test_failed_channel_raises_the_platform_error(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    config: PlanerConfig = make_config()
    fake_platform.fail_describe["yt_ua"] = PlatformError("backendError", "503")
    platform, book = _started(fake_platform, planer_paths, config)
    assert book.channel(config.channels[0]).status is ChannelStatus.FAILED
    with pytest.raises(PlatformError, match="backendError"):
        platform.get_stream(config.channels[0], "S1")
    assert fake_platform.stream_calls == []


def test_refused_channel_fails_only_its_own_objects(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Инвариант 9: канал, где дважды выбран чужой канал, — ошибка его объектов, второй канал работает."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    config: PlanerConfig = make_config()
    fake_platform.tokens_missing = {"yt_ua"}
    wrong: ChannelInfo = replace(
        FakePlatform.default_channel_info(config.channels[0]), handle_raw="@chuzhoy", title="Чужой канал"
    )
    fake_platform.login_answers["yt_ua"] = [wrong, wrong]
    platform, book = _started(fake_platform, planer_paths, config)
    (planer_paths.secrets_dir / "@yt_ua.token.json").unlink()
    book.check_without_login(config)
    outcome: RunOutcome = run(
        RunMode.FULL, config, planer_paths, platform, FakeFormSender(), now, rng, logins=book
    )
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None
    errors = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.ERROR]
    assert [(item.account_name, item.handle) for item in errors] == [("yt_ua", "@yt_ua")]
    assert errors[0].error is not None and errors[0].error.code == ERROR_CHANNEL_HANDLE_MISMATCH
    assert [call.channel_id for call in fake_platform.created] == ["yt_ru"]
    assert not (planer_paths.secrets_dir / "@yt_ua.token.json").exists()


def test_channels_without_objects_are_not_touched(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Слоты только на uk — у канала ru списка эфиров не спрашиваем."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    config: PlanerConfig = make_config()
    platform, book = _started(fake_platform, planer_paths, config)
    run(RunMode.DRY_RUN, config, planer_paths, platform, FakeFormSender(), now, rng, logins=book)
    assert fake_platform.list_calls == ["yt_ua"]


def test_notices_carry_channel_warnings_of_the_run(
    planer_paths: PlanerPaths, make_config: ConfigFactory, fake_platform: FakePlatform
) -> None:
    sync: ChannelSync = ChannelSync(fake_platform, planer_paths, FIXED_NOW)
    platform: VerifiedPlatform = VerifiedPlatform(fake_platform, ChannelBook(fake_platform, sync))
    fake_platform.seed_undated_broadcast("yt_ua", "Без времени")
    sync.add_warning("предупреждение канала")
    kinds = [notice.kind for notice in platform.take_notices()]
    assert kinds == [PlatformNoticeKind.UNDATED_BROADCAST, PlatformNoticeKind.CHANNEL]
