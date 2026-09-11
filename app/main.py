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
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from app.config.loader import ConfigError, PlanerConfig, ensure_config_exists, load_planer_config
from app.form.base import FormSender, NoopFormSender
from app.observability.logging_setup import close_logging, get_logger, setup_logging
from app.paths import PlanerPaths, build_paths, ensure_dirs, resolve_root
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.platforms.base import BroadcastPlatform
from app.platforms.fake import FakePlatform
from app.ui import messages_ru as msg

LOGGER = get_logger("main")

# Режимы, которых ещё нет, и этап, на котором они появятся.
PENDING_MODE_STAGES: Final[dict[str, str]] = {"--check": "3", "--auth": "3"}


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="planer", description=msg.CLI_DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help=msg.HELP_DRY_RUN)
    parser.add_argument("--debug", action="store_true", help=msg.HELP_DEBUG)
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help=msg.HELP_CHECK)
    modes.add_argument("--auth", metavar="CHANNEL_ID", help=msg.HELP_AUTH)
    modes.add_argument("--status", action="store_true", help=msg.HELP_STATUS)
    return parser


def build_dependencies() -> tuple[BroadcastPlatform, FormSender]:
    """Площадка и отправитель формы.

    Этап 3: FakePlatform → YouTubePlatform. Этап 4: NoopFormSender → отправитель Google-формы.
    """
    return FakePlatform(), NoopFormSender()


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


def _pending_mode(args: argparse.Namespace) -> str | None:
    if args.check:
        return "--check"
    if args.auth is not None:
        return "--auth"
    return None


def _requested_run_mode(args: argparse.Namespace) -> RunMode | None:
    """Полного запуска до этапа 3 нет: на фейке он записал бы выдуманные ключи в журнал."""
    if args.status:
        return RunMode.STATUS
    if args.dry_run:
        return RunMode.DRY_RUN
    return None


def _run(args: argparse.Namespace, paths: PlanerPaths) -> int:
    pending_mode: str | None = _pending_mode(args)
    if pending_mode is not None:
        _say(msg.MODE_NOT_AVAILABLE_YET.format(mode=pending_mode, stage=PENDING_MODE_STAGES[pending_mode]))
        return int(ExitCode.CONFIG)
    mode: RunMode | None = _requested_run_mode(args)
    if mode is None:
        _say(msg.FULL_RUN_NOT_AVAILABLE_YET)
        return int(ExitCode.CONFIG)
    config: PlanerConfig | None = _load_config(paths)
    if config is None:
        return int(ExitCode.CONFIG)
    platform, form_sender = build_dependencies()
    outcome: RunOutcome = run(
        mode,
        config,
        paths,
        platform,
        form_sender,
        datetime.now(timezone.utc),
        random.Random(),
        notice=msg.NOTICE_FAKE_PLATFORM,
    )
    _print_outcome(outcome, paths)
    return outcome.exit_code


def _load_config(paths: PlanerPaths) -> PlanerConfig | None:
    try:
        if not ensure_config_exists(paths):
            _say(msg.CONFIG_CREATED_FROM_EXAMPLE.format(path=paths.config_file))
            return None
        return load_planer_config(paths.config_file)
    except ConfigError as error:
        LOGGER.error("config_error key=%s problem=%s", error.key_path, error.problem)
        _say(str(error))
        return None


def _print_outcome(outcome: RunOutcome, paths: PlanerPaths) -> None:
    if outcome.problem is RunProblem.INBOX_EMPTY:
        _say(msg.INBOX_EMPTY.format(path=paths.inbox_dir))
    elif outcome.problem is RunProblem.REGISTRY_UNREADABLE:
        _say(msg.REGISTRY_UNREADABLE.format(path=paths.registry_file, error=outcome.problem_detail))
    if outcome.report_text is not None:
        _say(outcome.report_text)
    if outcome.report_path is not None:
        _say(msg.REPORT_WRITTEN.format(path=outcome.report_path))


if __name__ == "__main__":
    sys.exit(run_cli())
