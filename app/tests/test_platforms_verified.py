from __future__ import annotations

import random
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig
from app.output.report import OutcomeKind
from app.paths import PlanerPaths
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, run
from app.platforms.base import ChannelInfo, PlatformError
from app.platforms.fake import FakePlatform
from app.platforms.verified import ERROR_CHANNEL_NAME_MISMATCH, ChannelBindingError, VerifiedPlatform
from app.tests.conftest import FakeFormSender
from app.ui import messages_ru as msg

ConfigFactory = Callable[..., PlanerConfig]


class _Listener:
    def __init__(self) -> None:
        self.ready: list[str] = []

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        self.ready.append(channel.account_name)


def _verified(platform: FakePlatform, paths: PlanerPaths, listener: _Listener | None = None) -> VerifiedPlatform:
    return VerifiedPlatform(platform, paths.channels_file, listener=listener)


def _titled(platform: FakePlatform, account_name: str, title: str) -> None:
    platform.channel_info[account_name] = ChannelInfo(
        youtube_channel_id=f"UCfake{account_name}", title=title, default_language=None
    )


def test_matching_channel_is_described_once(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, listener)
    channel: ChannelConfig = make_config().channels[0]
    platform.list_upcoming(channel)
    platform.list_upcoming(channel)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert fake_platform.list_calls == ["yt_ua", "yt_ua"]
    assert listener.ready == ["yt_ua"]


def test_verification_writes_no_files(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    before: list[Path] = sorted(planer_paths.root.rglob("*"))
    _verified(fake_platform, planer_paths).list_upcoming(make_config().channels[0])
    assert sorted(planer_paths.root.rglob("*")) == before


def test_login_happens_at_first_access(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    fake_platform.tokens_missing = {"yt_ua"}
    logins: list[str] = []
    fake_platform.on_login = lambda channel: logins.append(channel.account_name)
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    assert logins == []
    platform.list_upcoming(make_config().channels[0])
    assert logins == ["yt_ua"]


@pytest.mark.parametrize(
    "title",
    ["Другой канал", "YT_UA", "yt_ua2"],
    ids=["other_channel", "case_differs", "longer_name"],
)
def test_title_mismatch_is_refused_until_end_of_run(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    title: str,
) -> None:
    _titled(fake_platform, "yt_ua", title)
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, listener)
    channel: ChannelConfig = make_config().channels[0]
    with pytest.raises(ChannelBindingError) as raised:
        platform.list_upcoming(channel)
    assert raised.value.code == ERROR_CHANNEL_NAME_MISMATCH
    assert raised.value.message == msg.AUTH_CHANNEL_NAME_MISMATCH.format(
        account_name="yt_ua", youtube_title=title, channels_file=planer_paths.channels_file
    )
    with pytest.raises(ChannelBindingError):
        platform.create_broadcast(channel, None)  # type: ignore[arg-type]   # до площадки не доходит
    assert fake_platform.describe_calls == ["yt_ua"]      # второй вызов на площадку не пошёл
    assert fake_platform.list_calls == [] and fake_platform.created == []
    assert listener.ready == []


def test_title_is_compared_in_nfc_without_edge_spaces(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """«й» из «и» и знака на YouTube — то же имя, что «й» одним символом в channels.json."""
    name: str = unicodedata.normalize("NFC", "Канал Лейла")
    decomposed: str = unicodedata.normalize("NFD", name)
    assert decomposed != name
    _titled(fake_platform, name, f"  {decomposed} ")
    channel: ChannelConfig = make_config(channels=((name, ["uk"]),)).channels[0]
    assert _verified(fake_platform, planer_paths).describe_channel(channel).title == f"  {decomposed} "


def test_platform_failure_is_not_remembered(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """Временный сбой площадки — не отказ: следующее обращение спрашивает канал снова."""
    fake_platform.fail_describe["yt_ua"] = PlatformError("backendError", "503")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    channel: ChannelConfig = make_config().channels[0]
    with pytest.raises(PlatformError):
        platform.describe_channel(channel)
    del fake_platform.fail_describe["yt_ua"]
    assert platform.describe_channel(channel).youtube_channel_id == "UCfakeyt_ua"


def test_refused_channel_fails_only_its_own_objects(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Инвариант 9: канал с чужим названием — ошибка его объектов в отчёте, второй канал работает."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    _titled(fake_platform, "yt_ua", "Чужой канал")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    outcome: RunOutcome = run(RunMode.FULL, make_config(), planer_paths, platform, FakeFormSender(), now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None
    errors = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.ERROR]
    assert [item.account_name for item in errors] == ["yt_ua"]
    assert errors[0].error is not None and errors[0].error.code == ERROR_CHANNEL_NAME_MISMATCH
    assert [call.channel_id for call in fake_platform.created] == ["yt_ru"]


def test_channels_without_objects_are_not_touched(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    now: datetime,
    rng: random.Random,
) -> None:
    """Слоты только на uk — канал ru не спрашивается."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths)
    run(RunMode.DRY_RUN, make_config(), planer_paths, platform, FakeFormSender(), now, rng)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert fake_platform.list_calls == ["yt_ua"]
