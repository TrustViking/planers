"""Точка входа планера: флаги (ТЗ §7.6), коды выхода (ТЗ §5.6).

Только разбор флагов, построение зависимостей и печать; оркестрация — app/pipeline/runner.py.
Вывод в консоль — только отсюда и только текстами из messages_ru.
"""
from __future__ import annotations

import argparse
import io
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import requests

from app.config.loader import (
    ChannelConfig,
    ConfigError,
    PlanerConfig,
    ensure_configs_exist,
    load_planer_config,
)
from app.form.base import FormSender
from app.form.discovery import FormDiscovery
from app.form.submitter import GoogleFormSender
from app.google.auth import AuthError, load_credentials, token_file_for
from app.observability.logging_setup import close_logging, get_logger, setup_logging
from app.paths import PlanerPaths, build_paths, ensure_dirs, resolve_root
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.youtube import YouTubePlatform
from app.state.channels import ChannelBinding, ChannelBindings, ChannelsStateError
from app.ui import messages_ru as msg

LOGGER = get_logger("main")

AUTH_ALL: Final[str] = "all"   # --auth all: все каналы из channels.yaml
LANGUAGES_JOINER: Final[str] = ", "


@dataclass(frozen=True)
class _Dependencies:
    config: PlanerConfig
    platform: BroadcastPlatform
    bindings: ChannelBindings


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="planer", description=msg.CLI_DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help=msg.HELP_DRY_RUN)
    parser.add_argument("--debug", action="store_true", help=msg.HELP_DEBUG)
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help=msg.HELP_CHECK)
    modes.add_argument("--auth", metavar="CHANNEL_KEY", help=msg.HELP_AUTH)
    modes.add_argument("--status", action="store_true", help=msg.HELP_STATUS)
    return parser


def build_platform(paths: PlanerPaths) -> BroadcastPlatform:
    """Боевая площадка; FakePlatform остаётся только для тестов."""
    return YouTubePlatform(paths.client_secret_file, paths.secrets_dir)


def build_form_sender(paths: PlanerPaths, now_utc: datetime) -> FormSender:
    """Отправитель Google-формы; адрес формы приходит в пакете, здесь его нет (ТЗ §7.5)."""
    session: requests.Session = requests.Session()
    return GoogleFormSender(session, FormDiscovery(session, paths.logs_dir, now_utc.astimezone()))


def run_cli(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    _configure_console()
    paths: PlanerPaths = build_paths(resolve_root())
    ensure_dirs(paths)
    log_path: Path = setup_logging(paths.logs_dir, debug=args.debug)
    LOGGER.info("run_started root=%s dry_run=%s status=%s log=%s", paths.root, args.dry_run, args.status, log_path)
    try:
        exit_code: int = _run(args, paths)
        LOGGER.info("run_finished exit_code=%d", exit_code)
        return exit_code
    finally:
        close_logging()


def _configure_console() -> None:
    """Символы, которых нет в кодировке консоли, — заменой, а не падением."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


def _say(text: str) -> None:
    print(text)


def _requested_run_mode(args: argparse.Namespace) -> RunMode:
    """Без флагов — полный цикл §4: создание и исправление эфиров."""
    if args.status:
        return RunMode.STATUS
    if args.dry_run:
        return RunMode.DRY_RUN
    return RunMode.FULL


def _run(args: argparse.Namespace, paths: PlanerPaths) -> int:
    dependencies: _Dependencies | None = _build_dependencies(paths)
    if dependencies is None:
        return int(ExitCode.CONFIG)
    if args.auth is not None:
        return _run_auth(args.auth, paths, dependencies)
    if args.check:
        return _run_check(paths, dependencies)
    if not (args.dry_run or args.status):
        _say(msg.FIRST_RUN_HEADER)
    return _run_pipeline(_requested_run_mode(args), paths, dependencies)


def _build_dependencies(paths: PlanerPaths) -> _Dependencies | None:
    """Конфиг, площадка и привязки; None — печатать уже нечего, код 2."""
    config: PlanerConfig | None = _load_config(paths)
    if config is None:
        return None
    if not paths.client_secret_file.is_file():
        LOGGER.error("client_secret_missing path=%s", paths.client_secret_file)
        _say(msg.CLIENT_SECRET_MISSING.format(path=paths.client_secret_file))
        return None
    try:
        bindings: ChannelBindings = ChannelBindings.load(paths.channels_state_file)
    except ChannelsStateError as error:
        LOGGER.error("channels_state_unreadable path=%s reason=%s", paths.channels_state_file, error)
        _say(msg.CHANNELS_STATE_UNREADABLE.format(path=paths.channels_state_file, error=error))
        return None
    return _Dependencies(config=config, platform=build_platform(paths), bindings=bindings)


def _load_config(paths: PlanerPaths) -> PlanerConfig | None:
    try:
        created: tuple[Path, ...] = ensure_configs_exist(paths)
        if created:
            for path in created:
                _say(msg.CONFIG_CREATED_FROM_EXAMPLE.format(path=path))
            return None
        return load_planer_config(paths.config_file, paths.channels_file)
    except ConfigError as error:
        LOGGER.error("config_error key=%s problem=%s", error.key_path, error.problem)
        _say(str(error))
        return None


def _run_auth(target: str, paths: PlanerPaths, dependencies: _Dependencies) -> int:
    channels: tuple[ChannelConfig, ...] | None = _auth_targets(target, paths, dependencies.config)
    if channels is None:
        return int(ExitCode.ERRORS)
    failed: int = 0
    for channel in channels:
        if not _authorize_channel(channel, paths, dependencies):
            failed += 1
    return int(ExitCode.ERRORS) if failed else int(ExitCode.OK)


def _auth_targets(
    target: str,
    paths: PlanerPaths,
    config: PlanerConfig,
) -> tuple[ChannelConfig, ...] | None:
    if target == AUTH_ALL:
        return config.channels
    channel: ChannelConfig | None = config.channel(target)
    if channel is None:
        _say(
            msg.AUTH_UNKNOWN_CHANNEL.format(
                path=paths.channels_file,
                key=target,
                known=LANGUAGES_JOINER.join(item.id for item in config.channels),
            )
        )
        return None
    return (channel,)


def _ensure_authorized(channel: ChannelConfig, paths: PlanerPaths, dependencies: _Dependencies) -> bool:
    """Токена нет — авторизуем прямо сейчас (ТЗ §5.3 п.1-2), браузер откроется сам."""
    if token_file_for(paths.secrets_dir, channel.id).is_file():
        return True
    _say(msg.CHECK_AUTHORIZING.format(key=channel.id))
    return _authorize_channel(channel, paths, dependencies)


def _authorize_channel(channel: ChannelConfig, paths: PlanerPaths, dependencies: _Dependencies) -> bool:
    _say(msg.AUTH_STARTING.format(key=channel.id, account_name=channel.account_name))
    _say(msg.AUTH_UNVERIFIED_APP_WARNING)
    _say(msg.AUTH_CHOOSE_RIGHT_CHANNEL.format(account_name=channel.account_name))
    try:
        load_credentials(
            paths.client_secret_file,
            token_file_for(paths.secrets_dir, channel.id),
            force_reauth=True,
        )
    except AuthError as error:
        _say_auth_error(channel.id, error.reason.value)
        return False
    try:
        info: ChannelInfo = dependencies.platform.describe_channel(channel)
    except PlatformError as error:
        _say(msg.CHECK_CHANNEL_FAILED.format(key=channel.id, code=error.code, message=error.message))
        _say(msg.AUTH_SCOPE_HINT)
        return False
    _say(msg.AUTH_OK.format(key=channel.id, title=info.title, youtube_channel_id=info.youtube_channel_id))
    return _remember_binding(channel, info, paths, dependencies.bindings)


def _say_auth_error(channel_key: str, reason: str) -> None:
    LOGGER.error("auth_failed channel=%s reason=%s", channel_key, reason)
    _say(msg.AUTH_FAILED.format(key=channel_key, reason=msg.AUTH_REASON_TEXT.get(reason, reason)))
    _say(msg.AUTH_SCOPE_HINT)


def _remember_binding(
    channel: ChannelConfig,
    info: ChannelInfo,
    paths: PlanerPaths,
    bindings: ChannelBindings,
) -> bool:
    """Чужой канал — ничего не переписываем; свой или новый — записываем привязку."""
    known: ChannelBinding | None = bindings.get(channel.id)
    if known is not None and known.youtube_channel_id != info.youtube_channel_id:
        _say(
            msg.AUTH_BINDING_MISMATCH.format(
                key=channel.id,
                expected_title=known.title,
                expected_id=known.youtube_channel_id,
                actual_title=info.title,
                actual_id=info.youtube_channel_id,
            )
        )
        return False
    bindings.upsert(
        ChannelBinding(
            channel_key=channel.id,
            youtube_channel_id=info.youtube_channel_id,
            title=info.title,
            authorized_at=datetime.now().astimezone().replace(tzinfo=None),
        )
    )
    try:
        bindings.save(paths.channels_state_file)
    except OSError as error:
        _say(msg.CHANNELS_STATE_UNREADABLE.format(path=paths.channels_state_file, error=error))
        return False
    if known is not None:
        _say(msg.AUTH_BINDING_UPDATED)
    else:
        _say(msg.AUTH_BINDING_SAVED.format(path=paths.channels_state_file))
    return True


def _run_check(paths: PlanerPaths, dependencies: _Dependencies) -> int:
    _say(msg.CHECK_HEADER.format(path=paths.channels_file))
    failed: int = 0
    for channel in dependencies.config.channels:
        if not _check_channel(channel, paths, dependencies):
            failed += 1
    _say(msg.CHECK_CHANNEL_LANGUAGE_NOTE)
    _say(msg.CHECK_HAS_PROBLEMS if failed else msg.CHECK_ALL_OK)
    return int(ExitCode.ERRORS) if failed else int(ExitCode.OK)


def _check_channel(channel: ChannelConfig, paths: PlanerPaths, dependencies: _Dependencies) -> bool:
    if not _ensure_authorized(channel, paths, dependencies):
        return False
    try:
        info: ChannelInfo = dependencies.platform.describe_channel(channel)
        mismatch: str | None = _binding_problem(channel, info, dependencies.bindings)
        if mismatch is not None:
            _say(mismatch)
            return False
        upcoming: int = len(dependencies.platform.list_upcoming(channel))
    except PlatformError as error:
        LOGGER.warning("check_failed channel=%s code=%s", channel.id, error.code)
        _say(msg.CHECK_CHANNEL_FAILED.format(key=channel.id, code=error.code, message=error.message))
        return False
    _say(
        msg.CHECK_CHANNEL_OK.format(
            key=channel.id,
            title=info.title,
            youtube_channel_id=info.youtube_channel_id,
            channel_language=info.default_language or msg.CHECK_CHANNEL_LANGUAGE_UNSET,
            languages=LANGUAGES_JOINER.join(channel.languages),
            upcoming=upcoming,
        )
    )
    return True


def _binding_problem(channel: ChannelConfig, info: ChannelInfo, bindings: ChannelBindings) -> str | None:
    """None — привязка на месте; иначе готовый текст для владельца (ТЗ §5.3)."""
    known: ChannelBinding | None = bindings.get(channel.id)
    if known is None:
        return msg.CHECK_NEEDS_AUTH.format(key=channel.id)
    if known.youtube_channel_id != info.youtube_channel_id:
        return msg.AUTH_BINDING_MISMATCH.format(
            key=channel.id,
            expected_title=known.title,
            expected_id=known.youtube_channel_id,
            actual_title=info.title,
            actual_id=info.youtube_channel_id,
        )
    return None


def _run_pipeline(mode: RunMode, paths: PlanerPaths, dependencies: _Dependencies) -> int:
    if not _bindings_verified(paths, dependencies):
        _say(msg.BINDINGS_NOT_VERIFIED)
        return int(ExitCode.CONFIG)
    now_utc: datetime = datetime.now(timezone.utc)
    outcome: RunOutcome = run(
        mode,
        dependencies.config,
        paths,
        dependencies.platform,
        build_form_sender(paths, now_utc),
        now_utc,
        random.Random(),
    )
    _print_outcome(outcome, paths)
    return outcome.exit_code


def _bindings_verified(paths: PlanerPaths, dependencies: _Dependencies) -> bool:
    """Перед чтением и записью — токен каждого канала ведёт на тот же канал (ТЗ §5.3)."""
    for channel in dependencies.config.channels:
        if not _ensure_authorized(channel, paths, dependencies):
            return False
        try:
            info: ChannelInfo = dependencies.platform.describe_channel(channel)
        except PlatformError as error:
            LOGGER.error("binding_check_failed channel=%s code=%s", channel.id, error.code)
            _say(msg.CHECK_CHANNEL_FAILED.format(key=channel.id, code=error.code, message=error.message))
            return False
        problem: str | None = _binding_problem(channel, info, dependencies.bindings)
        if problem is not None:
            _say(problem)
            return False
    return True


def _print_outcome(outcome: RunOutcome, paths: PlanerPaths) -> None:
    if outcome.problem is RunProblem.PROMO_EMPTY:
        _say(msg.PROMO_EMPTY.format(path=paths.promo_dir))
    elif outcome.problem is RunProblem.REGISTRY_UNREADABLE:
        _say(msg.REGISTRY_UNREADABLE.format(path=paths.registry_file, error=outcome.problem_detail))
    if outcome.report_text is not None:
        _say(outcome.report_text)
    if outcome.report_path is not None:
        _say(msg.REPORT_WRITTEN.format(path=outcome.report_path))


if __name__ == "__main__":
    sys.exit(run_cli())
