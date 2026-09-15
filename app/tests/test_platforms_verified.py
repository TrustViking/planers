from __future__ import annotations

import json
import random
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
from app.platforms.verified import (
    ERROR_BINDING_MISMATCH,
    ERROR_CHANNEL_TAKEN,
    ChannelBindingError,
    VerifiedPlatform,
)
from app.state.channels import ChannelBinding, ChannelBindings
from app.tests.conftest import FakeFormSender

AUTHORIZED_AT: datetime = datetime(2027, 3, 16, 12, 0)
ConfigFactory = Callable[..., PlanerConfig]


class _Listener:
    def __init__(self) -> None:
        self.ready: list[tuple[str, bool]] = []

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo, *, is_new_binding: bool) -> None:
        self.ready.append((channel.account_name, is_new_binding))


def _bindings(**youtube_ids: str) -> ChannelBindings:
    bindings: ChannelBindings = ChannelBindings()
    for account_name, youtube_channel_id in youtube_ids.items():
        bindings.upsert(ChannelBinding(account_name, youtube_channel_id, f"Fake {account_name}", AUTHORIZED_AT))
    return bindings


def _verified(
    platform: FakePlatform,
    paths: PlanerPaths,
    bindings: ChannelBindings,
    listener: _Listener | None = None,
) -> VerifiedPlatform:
    return VerifiedPlatform(platform, bindings, paths.bindings_file, listener=listener, clock=lambda: AUTHORIZED_AT)


def test_new_channel_is_described_once_and_bound(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    listener: _Listener = _Listener()
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, ChannelBindings(), listener)
    channel: ChannelConfig = make_config().channels[0]
    platform.list_upcoming(channel)
    platform.list_upcoming(channel)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert listener.ready == [("yt_ua", True)]
    payload: dict[str, Any] = json.loads(planer_paths.bindings_file.read_text(encoding="utf-8"))
    assert payload["channels"]["yt_ua"]["youtube_channel_id"] == "UCfakeyt_ua"


def test_login_happens_at_first_access(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    fake_platform.tokens_missing = {"yt_ua"}
    logins: list[str] = []
    fake_platform.on_login = lambda channel: logins.append(channel.account_name)
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, ChannelBindings())
    assert logins == []
    platform.list_upcoming(make_config().channels[0])
    assert logins == ["yt_ua"]


@pytest.mark.parametrize(
    ("known", "code"),
    [
        ({"yt_ua": "UCsomeoneElse"}, ERROR_BINDING_MISMATCH),
        ({"Другое имя": "UCfakeyt_ua"}, ERROR_CHANNEL_TAKEN),
    ],
    ids=["token_leads_elsewhere", "youtube_channel_under_other_name"],
)
def test_wrong_channel_is_refused_and_nothing_is_rewritten(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
    known: dict[str, str],
    code: str,
) -> None:
    bindings: ChannelBindings = _bindings(**known)
    bindings.save(planer_paths.bindings_file)
    before: str = planer_paths.bindings_file.read_text(encoding="utf-8")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, bindings)
    channel: ChannelConfig = make_config().channels[0]
    with pytest.raises(ChannelBindingError) as raised:
        platform.list_upcoming(channel)
    assert raised.value.code == code
    assert "yt_ua" in raised.value.message
    with pytest.raises(ChannelBindingError):
        platform.create_broadcast(channel, None)  # type: ignore[arg-type]   # до площадки не доходит
    assert fake_platform.list_calls == [] and fake_platform.describe_calls == ["yt_ua"]
    assert planer_paths.bindings_file.read_text(encoding="utf-8") == before


def test_platform_failure_is_not_remembered(
    planer_paths: PlanerPaths,
    make_config: ConfigFactory,
    fake_platform: FakePlatform,
) -> None:
    """Временный сбой площадки — не отказ: следующее обращение спрашивает канал снова."""
    fake_platform.fail_describe["yt_ua"] = PlatformError("backendError", "503")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, ChannelBindings())
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
    """Инвариант 9: канал за чужим токеном — ошибка его объектов в отчёте, второй канал работает."""
    make_package(
        planer_paths.bcast_dir,
        slots=[make_slot("17-03-2027", "19:00", "uk"), make_slot("17-03-2027", "19:00", "ru")],
    )
    bindings: ChannelBindings = _bindings(yt_ua="UCsomeoneElse")
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, bindings)
    outcome: RunOutcome = run(RunMode.FULL, make_config(), planer_paths, platform, FakeFormSender(), now, rng)
    assert outcome.exit_code == ExitCode.ERRORS
    assert outcome.report is not None
    errors = [item for item in outcome.report.outcomes if item.kind is OutcomeKind.ERROR]
    assert [item.account_name for item in errors] == ["yt_ua"]
    assert errors[0].error is not None and errors[0].error.code == ERROR_BINDING_MISMATCH
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
    """Слоты только на uk — канал ru не спрашивается и не привязывается."""
    make_package(planer_paths.bcast_dir, slots=[make_slot("17-03-2027", "19:00", "uk")])
    platform: VerifiedPlatform = _verified(fake_platform, planer_paths, ChannelBindings())
    run(RunMode.DRY_RUN, make_config(), planer_paths, platform, FakeFormSender(), now, rng)
    assert fake_platform.describe_calls == ["yt_ua"]
    assert fake_platform.list_calls == ["yt_ua"]
