"""Точка входа планера: флаги (ТЗ §7.6), коды выхода (ТЗ §5.6).

Только разбор флагов, построение зависимостей и печать; оркестрация — app/pipeline/runner.py.
Вывод в консоль — только отсюда и только текстами из messages_ru: шапка сразу после настройки логов
(до конфигов и сети), строки прогресса по ходу работы (ConsoleProgress), пустая строка, итоговые блоки.
Порядок запуска: конфиги → проверка каналов без браузера (ChannelBook.check_without_login: статус каждого
канала — READY / NEEDS_LOGIN / REFUSED / FAILED; ник, название и файл токена выравниваются по id YouTube,
чужой токен удаляется) → пакеты из bcast\\ → чтение форм → объекты → фаза входов: каналы с объектами
и статусом NEEDS_LOGIN входят подряд (токен пишется только после подтверждения канала) → сверка и действия
только по каналам READY (app/platforms/verified.py). После фазы входов браузер не открывается.
--status и --check: проверка без браузера → фаза входов по всем каналам → работа. --auth: вход без проверки
при старте; прежний токен остаётся, пока новый вход не подтверждён.
Обрыв (Ctrl+C) и любое необработанное исключение ловятся в run_cli: строка в лог и в консоль, код 1.
"""

from __future__ import annotations

import argparse
import io
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import requests

from app.config.loader import (
    AUTH_ALL,
    ChannelConfig,
    ConfigError,
    Platform,
    PlanerConfig,
    PlanerSettings,
    Privacy,
    allowed_values,
    load_planer_config,
)
from app.core.dates import format_datetime_text
from app.form.base import FormSender
from app.form.discovery import FormDiscovery
from app.form.submitter import GoogleFormSender
from app.observability.logging_setup import close_logging, get_logger, setup_logging
from app.output.console import render_console
from app.output.progress import ConsoleProgress
from app.paths import PlanerPaths, build_paths, ensure_dirs, resolve_root
from app.pipeline.runner import ExitCode, RunMode, RunOutcome, RunProblem, run
from app.platforms.base import BroadcastPlatform, ChannelInfo, PlatformError
from app.platforms.channel import Channel, ChannelBindingError, ChannelBook, ChannelStatus, youtube_handle_text
from app.platforms.channel_sync import ChannelSync
from app.platforms.verified import VerifiedPlatform
from app.platforms.youtube import YouTubePlatform
from app.ui import messages_ru as msg
from app.version import APP_VERSION

LOGGER = get_logger("main")

LIST_JOINER: Final[str] = ", "
REASON_SEPARATOR: Final[str] = ":"   # сообщение отказа входа: «<AuthErrorReason>: подробности»
# Шапка запуска: --check и --auth — обычная, как у полного цикла.
TITLES: Final[dict[RunMode, str]] = {
    RunMode.FULL: msg.CONSOLE_TITLE,
    RunMode.DRY_RUN: msg.CONSOLE_TITLE_DRY_RUN,
    RunMode.STATUS: msg.CONSOLE_TITLE_STATUS,
}


class ChannelConsole:
    """Вход в канал глазами владельца: ChannelBook зовёт это в фазе входов, в --check и в --auth."""

    def on_login(self, channel: ChannelConfig) -> None:
        """Ровно перед открытием браузера: какой канал выбирать."""
        names: dict[str, str] = {"account_name": channel.account_name, "handle": channel.handle}
        _say(msg.AUTH_STARTING.format(**names))
        _say(msg.AUTH_CHOOSE_ACCOUNT.format(google_account=channel.google_account, **names))
        _say(msg.AUTH_CHOOSE_RIGHT_CHANNEL.format(**names))
        _say(msg.AUTH_UNVERIFIED_APP_WARNING)

    def on_wrong_channel(self, channel: ChannelConfig, info: ChannelInfo, will_retry: bool) -> None:
        template: str = msg.AUTH_WRONG_CHANNEL_RETRY if will_retry else msg.AUTH_WRONG_CHANNEL_GIVE_UP
        _say(
            template.format(
                account_name=channel.account_name,
                handle=channel.handle,
                youtube_title=info.title,
                youtube_handle=youtube_handle_text(info),
            )
        )

    def on_login_failed(self, channel: ChannelConfig, error: PlatformError) -> None:
        reason: str = error.message.split(REASON_SEPARATOR, 1)[0]
        _say(
            msg.AUTH_FAILED.format(
                account_name=channel.account_name,
                handle=channel.handle,
                reason=msg.AUTH_REASON_TEXT.get(reason, error.message),
            )
        )
        _say(msg.AUTH_SCOPE_HINT)

    def on_channel_ready(self, channel: ChannelConfig, info: ChannelInfo) -> None:
        _say(
            msg.AUTH_OK.format(
                account_name=channel.account_name,
                handle=channel.handle,
                title=info.title,
                youtube_handle=youtube_handle_text(info),
                youtube_channel_id=info.youtube_channel_id,
            )
        )


@dataclass(frozen=True)
class _Dependencies:
    config: PlanerConfig
    platform: VerifiedPlatform
    book: ChannelBook
    rng: random.Random


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(prog="planer", description=msg.CLI_DESCRIPTION)
    parser.add_argument("--dry-run", action="store_true", help=msg.HELP_DRY_RUN)
    parser.add_argument("--debug", action="store_true", help=msg.HELP_DEBUG)
    parser.add_argument(
        "--version",
        action="version",
        version=msg.VERSION_TEXT.format(version=APP_VERSION),
        help=msg.HELP_VERSION,
    )
    modes: argparse._MutuallyExclusiveGroup = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help=msg.HELP_CHECK)
    modes.add_argument("--auth", metavar="HANDLE", help=msg.HELP_AUTH)
    modes.add_argument("--status", action="store_true", help=msg.HELP_STATUS)
    return parser


def build_platform(paths: PlanerPaths, settings: PlanerSettings, rng: random.Random) -> BroadcastPlatform:
    """Боевая площадка; FakePlatform остаётся только для тестов.

    Из planer.json площадка берёт только паузу между обращениями; настройки эфира она получает спекой.
    """
    return YouTubePlatform(
        paths.client_secret_file,
        paths.secrets_dir,
        request_pause_sec=settings.youtube_pause_seconds,
        rng=rng,
    )


def build_form_sender(paths: PlanerPaths, now_utc: datetime, rng: random.Random) -> FormSender:
    """Отправитель Google-формы; адрес формы приходит в пакете, здесь его нет (ТЗ §7.5)."""
    session: requests.Session = requests.Session()
    discovery: FormDiscovery = FormDiscovery(session, paths.logs_dir, now_utc.astimezone(), rng)  # type: ignore
    return GoogleFormSender(session, discovery, rng)  # type: ignore


def run_cli(argv: Sequence[str] | None = None) -> int:
    args: argparse.Namespace = build_parser().parse_args(argv)
    _configure_console()
    paths: PlanerPaths = build_paths(resolve_root())
    ensure_dirs(paths)
    log_path: Path = setup_logging(paths.logs_dir, debug=args.debug)
    LOGGER.info(
        "run_started version=%s root=%s dry_run=%s status=%s log=%s",
        APP_VERSION,
        paths.root,
        args.dry_run,
        args.status,
        log_path,
    )
    now_utc: datetime = datetime.now(timezone.utc)
    _say_title(args, now_utc)
    try:
        exit_code: int = _run_guarded(args, paths, log_path, now_utc)
        LOGGER.info("run_finished exit_code=%d", exit_code)
        return exit_code
    finally:
        close_logging()


def _run_guarded(args: argparse.Namespace, paths: PlanerPaths, log_path: Path, now_utc: datetime) -> int:
    """Обрыв и падение не пропадают без следа: причина — в лог, короткая строка — в консоль, код 1."""
    try:
        return _run(args, paths, log_path, now_utc)
    except KeyboardInterrupt:
        LOGGER.warning("run_interrupted")
        _say(msg.RUN_INTERRUPTED)
    # Единственный перехват Exception в планере: всё, что не обработано ниже, — ошибка программы.
    # Без него трассировка ушла бы только в окно консоли, которое владелец закроет, а лог остался бы
    # оборванным на последней строке (живой прогон 17-09-2026 00:38 и 00:50).
    except Exception:
        LOGGER.exception("run_crashed log=%s", log_path)
        _say(msg.RUN_CRASHED.format(log=log_path))
    return int(ExitCode.ERRORS)


def _configure_console() -> None:
    """Символы, которых нет в кодировке консоли, — заменой, а не падением."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


def _say(text: str) -> None:
    """flush — чтобы строка была видна сразу и в собранном exe, а не в конце запуска."""
    print(text, flush=True)


def _say_title(args: argparse.Namespace, now_utc: datetime) -> None:
    """Шапка — первая строка любого запуска; время — то же, что в отчёте этого запуска."""
    mode: RunMode = RunMode.FULL if args.check or args.auth is not None else _requested_run_mode(args)
    _say(TITLES[mode].format(version=APP_VERSION, generated_at=format_datetime_text(now_utc.astimezone())))


def _requested_run_mode(args: argparse.Namespace) -> RunMode:
    """Без флагов — полный цикл §4: создание и исправление эфиров."""
    if args.status:
        return RunMode.STATUS
    if args.dry_run:
        return RunMode.DRY_RUN
    return RunMode.FULL


def _run(args: argparse.Namespace, paths: PlanerPaths, log_path: Path, now_utc: datetime) -> int:
    dependencies: _Dependencies | None = _build_dependencies(paths, now_utc)
    if dependencies is None:
        return int(ExitCode.CONFIG)
    if args.auth is not None:
        return _run_auth(args.auth, paths, dependencies)
    checked: tuple[_Dependencies, list[str]] | None = _check_channels(dependencies)
    if checked is None:
        return int(ExitCode.CONFIG)
    dependencies, channel_warnings = checked
    if args.check:
        return _run_check(paths, dependencies, channel_warnings)
    return _run_pipeline(_requested_run_mode(args), paths, dependencies, log_path, now_utc, channel_warnings)


def _build_dependencies(paths: PlanerPaths, now_utc: datetime) -> _Dependencies | None:
    """Конфиги и паспорт программы; к каналам здесь никто не обращается. None — код 2."""
    config: PlanerConfig | None = _load_config(paths)
    if config is None:
        return None
    if not paths.client_secret_file.is_file():
        LOGGER.error("client_secret_missing path=%s", paths.client_secret_file)
        _say(msg.CLIENT_SECRET_MISSING.format(path=paths.client_secret_file))
        return None
    rng: random.Random = random.Random()
    youtube: BroadcastPlatform = build_platform(paths, config.settings, rng)
    # проверка при старте, входы и шлюз площадки — через одну площадку, один паспорт и одну книгу каналов
    sync: ChannelSync = ChannelSync(youtube, paths, now_utc.astimezone())
    book: ChannelBook = ChannelBook(youtube, sync, ChannelConsole())
    return _Dependencies(config=config, platform=VerifiedPlatform(youtube, book), book=book, rng=rng)


def _check_channels(dependencies: _Dependencies) -> tuple[_Dependencies, list[str]] | None:
    """Проверка каналов без браузера: конфиг после выравнивания и предупреждения запуска. None — код 2."""
    try:
        config, warnings = dependencies.book.check_without_login(dependencies.config)
    except ConfigError as error:
        _say_config_error(error)
        return None
    return replace(dependencies, config=config), warnings


def _load_config(paths: PlanerPaths) -> PlanerConfig | None:
    """Нет файла или поля — ошибка и точный шаблон файла; конфиги в secrets\\ планер не пишет."""
    try:
        return load_planer_config(paths.config_file, paths.channels_file)
    except ConfigError as error:
        _say_config_error(error)
        if error.is_template_needed:
            _say_template(error.config_path, paths)
        return None


def _say_config_error(error: ConfigError) -> None:
    LOGGER.error(
        "config_error path=%s key=%s kind=%s problem=%s",
        error.config_path,
        error.key_path,
        error.kind.value,
        error.problem,
    )
    _say(str(error))


def _say_template(config_path: Path, paths: PlanerPaths) -> None:
    if config_path == paths.channels_file:
        _say(msg.CONFIG_CHANNELS_HINT.format(path=config_path))
        _say_channels_fields()
        _say(msg.CONFIG_CHANNELS_TEMPLATE)
        return
    _say(msg.CONFIG_PLANER_HINT.format(path=config_path))
    _say(msg.CONFIG_PLANER_TEMPLATE)


def _say_channels_fields() -> None:
    """Что вписать в поля channels.json; допустимые значения — те же, что проверяет loader."""
    for line in msg.CONFIG_CHANNELS_FIELDS:
        _say(
            line.format(
                languages=msg.CONFIG_LANGUAGES_RULE,
                privacy=allowed_values(Privacy),
                platform=allowed_values(Platform),
            )
        )


def _run_auth(target: str, paths: PlanerPaths, dependencies: _Dependencies) -> int:
    """Принудительный вход: прежний токен заменяется только подтверждённым входом (правило то же, что в запуске)."""
    channels: tuple[ChannelConfig, ...] | None = _auth_targets(target, paths, dependencies.config)
    if channels is None:
        return int(ExitCode.ERRORS)
    failed: int = 0
    for channel_config in channels:
        channel: Channel = dependencies.book.log_in(channel_config, force=True)
        if channel.status is not ChannelStatus.READY:
            failed += 1
            _say_not_ready(channel)
    _say_warnings(dependencies.book.take_warnings())
    return int(ExitCode.ERRORS) if failed else int(ExitCode.OK)


def _auth_targets(
    target: str,
    paths: PlanerPaths,
    config: PlanerConfig,
) -> tuple[ChannelConfig, ...] | None:
    if target == AUTH_ALL:
        return config.channels
    channel: ChannelConfig | None = config.channel_by_handle(target)
    if channel is None:
        _say(
            msg.AUTH_UNKNOWN_CHANNEL.format(
                path=paths.channels_file,
                handle=target,
                known=LIST_JOINER.join(
                    msg.CHANNEL_LISTED.format(handle=item.handle, account_name=item.account_name)
                    for item in config.channels
                ),
            )
        )
        return None
    return (channel,)


def _say_not_ready(channel: Channel) -> None:
    """Отказ — готовым текстом; сбой входа уже напечатан в момент входа (AUTH_FAILED)."""
    if isinstance(channel.error, ChannelBindingError):
        _say(msg.CHECK_CHANNEL_REFUSED.format(message=channel.error.message))
        return
    if channel.status is ChannelStatus.FAILED and channel.login_attempts:
        return
    error: PlatformError | None = channel.access_error()
    if error is not None:
        _say_channel_failed(channel.config, error)


def _say_channel_failed(channel: ChannelConfig, error: PlatformError) -> None:
    LOGGER.warning(
        'check_failed channel="%s" handle=%s code=%s', channel.account_name, channel.handle, error.code
    )
    _say(
        msg.CHECK_CHANNEL_FAILED.format(
            account_name=channel.account_name, handle=channel.handle, code=error.code, message=error.message
        )
    )


def _say_warnings(warnings: Sequence[str]) -> None:
    for warning in warnings:
        _say(msg.CONSOLE_ATTENTION_TEXT.format(text=warning))


def _run_check(paths: PlanerPaths, dependencies: _Dependencies, channel_warnings: Sequence[str]) -> int:
    """Все каналы конфига — тем же путём, что и запуск: проверка без браузера, фаза входов, работа."""
    _say(msg.CHECK_HEADER.format(path=paths.channels_file))
    _say_warnings(channel_warnings)
    dependencies.book.log_in_needed(dependencies.config.channels)
    failed: int = 0
    for channel in dependencies.config.channels:
        if not _check_channel(channel, dependencies):
            failed += 1
    _say_warnings(dependencies.book.take_warnings())
    _say(msg.CHECK_CHANNEL_LANGUAGE_NOTE)
    _say(msg.CHECK_HAS_PROBLEMS if failed else msg.CHECK_ALL_OK)
    return int(ExitCode.ERRORS) if failed else int(ExitCode.OK)


def _check_channel(channel_config: ChannelConfig, dependencies: _Dependencies) -> bool:
    channel: Channel = dependencies.book.channel(channel_config)
    if channel.status is not ChannelStatus.READY or channel.info is None:
        _say_not_ready(channel)
        return False
    info: ChannelInfo = channel.info
    try:
        upcoming: int = len(dependencies.platform.list_upcoming(channel_config))
    except PlatformError as error:
        _say_channel_failed(channel_config, error)
        return False
    _say(
        msg.CHECK_CHANNEL_OK.format(
            account_name=channel_config.account_name,
            handle=channel_config.handle,
            title=info.title,
            youtube_handle=youtube_handle_text(info),
            youtube_channel_id=info.youtube_channel_id,
            channel_language=info.default_language or msg.CHECK_CHANNEL_LANGUAGE_UNSET,
            languages=LIST_JOINER.join(channel_config.languages),
            upcoming=upcoming,
        )
    )
    return True


def _run_pipeline(
    mode: RunMode,
    paths: PlanerPaths,
    dependencies: _Dependencies,
    log_path: Path,
    now_utc: datetime,
    channel_warnings: Sequence[str],
) -> int:
    outcome: RunOutcome = run(
        mode,
        dependencies.config,
        paths,
        dependencies.platform,
        build_form_sender(paths, now_utc, dependencies.rng),
        now_utc,
        dependencies.rng,
        progress=ConsoleProgress(),
        channel_warnings=channel_warnings,
        logins=dependencies.book,
    )
    channel_order: tuple[str, ...] = tuple(channel.key for channel in dependencies.config.channels)
    _print_outcome(outcome, paths, log_path, channel_order)
    return outcome.exit_code


def _print_outcome(outcome: RunOutcome, paths: PlanerPaths, log_path: Path, channel_order: tuple[str, ...]) -> None:
    """Консоль — блоки и пути; подробности — в отчёте, диагностика — в логе (ТЗ §5.6).

    Пустая строка отделяет прогресс от итога; шапка уже напечатана при старте.
    """
    _say("")
    if outcome.problem is RunProblem.BCAST_EMPTY:
        _say(msg.BCAST_EMPTY.format(path=paths.bcast_dir))
    if outcome.report is not None:
        _say(
            render_console(
                outcome.report,
                root=paths.root,
                report_path=outcome.report_path,
                log_path=log_path,
                channel_order=channel_order,
            )
        )


if __name__ == "__main__":
    sys.exit(run_cli())
