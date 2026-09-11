"""Точка входа планера: флаги (ТЗ §7.6), коды выхода (ТЗ §5.6).

Этап 2a: inbox → отбор пар → отчёт; сверка с YouTube, ключи и форма — этапы 2b–4.
Вывод в консоль — только отсюда и только текстами из messages_ru.
"""
from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Final

from app.config.loader import ConfigError, PlanerConfig, ensure_config_exists, load_planer_config
from app.core.dates import format_now_local
from app.observability.logging_setup import close_logging, get_logger, setup_logging
from app.output.report import ArchiveOutcome, build_run_report, render_report, write_report
from app.package.inbox import InboxScan, archive_package, cleanup_expired, scan_inbox
from app.paths import PlanerPaths, build_paths, ensure_dirs, resolve_root
from app.pipeline.selection import Selection, select_pairs
from app.ui import messages_ru as msg

LOGGER = get_logger("main")


class ExitCode(IntEnum):
    OK = 0            # всё, что можно было сделать, сделано
    ERRORS = 1        # есть ошибки
    CONFIG = 2        # ошибка конфигурации/авторизации — ничего не делалось
    INBOX_EMPTY = 3   # в inbox\ нет пакетов


# Режимы, которых ещё нет, и этап, на котором они появятся.
PENDING_MODE_STAGES: Final[dict[str, str]] = {"--check": "3", "--auth": "3", "--status": "2b"}


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="planer", description=msg.CLI_DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help=msg.HELP_DRY_RUN)
    parser.add_argument("--debug", action="store_true", help=msg.HELP_DEBUG)
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help=msg.HELP_CHECK)
    modes.add_argument("--auth", metavar="CHANNEL_ID", help=msg.HELP_AUTH)
    modes.add_argument("--status", action="store_true", help=msg.HELP_STATUS)
    return parser


def run_cli(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    _configure_console()
    paths: PlanerPaths = build_paths(resolve_root())
    ensure_dirs(paths)
    log_path: Path = setup_logging(paths.logs_dir, debug=args.debug)
    LOGGER.info("run_started root=%s dry_run=%s log=%s", paths.root, args.dry_run, log_path)
    try:
        exit_code: ExitCode = _run(args, paths)
        LOGGER.info("run_finished exit_code=%d", int(exit_code))
        return int(exit_code)
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
    if args.status:
        return "--status"
    return None


def _run(args: argparse.Namespace, paths: PlanerPaths) -> ExitCode:
    pending_mode: str | None = _pending_mode(args)
    if pending_mode is not None:
        _say(msg.MODE_NOT_AVAILABLE_YET.format(mode=pending_mode, stage=PENDING_MODE_STAGES[pending_mode]))
        return ExitCode.CONFIG
    config: PlanerConfig | None = _load_config(paths)
    if config is None:
        return ExitCode.CONFIG
    now: datetime = datetime.now(timezone.utc)
    scan: InboxScan = scan_inbox(paths, now)
    if scan.is_empty:
        _say(msg.INBOX_EMPTY.format(path=paths.inbox_dir))
        return ExitCode.INBOX_EMPTY
    return _process_inbox(paths=paths, config=config, scan=scan, now=now, dry_run=args.dry_run)


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


def _process_inbox(
    *,
    paths: PlanerPaths,
    config: PlanerConfig,
    scan: InboxScan,
    now: datetime,
    dry_run: bool,
) -> ExitCode:
    selection: Selection = select_pairs(scan.slot_map, config, now)
    outcome: ArchiveOutcome = ArchiveOutcome() if dry_run else _archive_all_past(paths, scan)
    report_text: str = render_report(
        build_run_report(
            scan=scan,
            selection=selection,
            config=config,
            archive_outcome=outcome,
            generated_at_text=format_now_local(),
        )
    )
    _say(report_text)
    report_path: Path = write_report(paths, report_text, datetime.now().astimezone())
    _say(msg.REPORT_WRITTEN.format(path=report_path))
    if not dry_run:
        cleanup_expired(paths, config.inbox_keep_days, now)
    return ExitCode.ERRORS if scan.problems or outcome.failed else ExitCode.OK


def _archive_all_past(paths: PlanerPaths, scan: InboxScan) -> ArchiveOutcome:
    moved: set[Path] = set()
    failed: dict[Path, str] = {}
    for package in scan.all_past_packages:
        try:
            archive_package(paths, package)
        except OSError as error:
            LOGGER.error("package_archive_failed file=%s reason=%s", package.path.name, error)
            failed[package.path] = str(error)
            continue
        moved.add(package.path)
    return ArchiveOutcome(moved=frozenset(moved), failed=failed)


if __name__ == "__main__":
    sys.exit(run_cli())
