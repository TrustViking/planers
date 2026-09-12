"""Оркестрация запуска (ТЗ §4): promo → отбор → сверка → действия → журнал → keys.txt → переносы → отчёт.

main.py только разбирает флаги, строит зависимости и печатает результат.
Тексты для владельца здесь не собираются: исходы — данными, текст — в output/.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Final

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.dates import format_datetime_text
from app.core.retention import cleanup_expired
from app.form.base import FormSender, FormSendResult
from app.observability.logging_setup import get_logger, mask_stream_key
from app.output.keys_file import (
    KeyRow,
    key_row_from_platform,
    key_row_from_registration,
    render_keys_file,
    write_keys_file,
)
from app.output.report import (
    ERROR_OUTCOME_KINDS,
    FormState,
    OrphanLine,
    OutcomeError,
    OutcomeKind,
    PairOutcome,
    RunMode,
    RunReport,
    build_package_lines,
    build_skipped_lines,
    render_report,
    write_report,
)
from app.package.promo import PromoScan, scan_promo
from app.package.model import Package, PackageError, Slot, read_preview
from app.paths import PlanerPaths
from app.pipeline.reconciler import (
    Decision,
    MarkedScan,
    OrphanBroadcast,
    ReconciledPair,
    Reconciler,
    Reconciliation,
    split_marker,
)
from app.pipeline.selection import Selection, select_pairs
from app.platforms.base import BroadcastPlatform, PlatformError, broadcast_url_for
from app.state.registry import FormStatus, Registration, Registry, RegistryError

__all__ = ["ExitCode", "RunMode", "RunOutcome", "RunProblem", "run"]

LOGGER = get_logger("runner")
# OutcomeError.origin для ошибок самого планера и его файлов; тексты — messages_ru.
ERROR_ORIGIN_PLANER: Final[str] = "planer"
ERROR_ORIGIN_REGISTRY: Final[str] = "registry"
ERROR_ORIGIN_PACKAGE: Final[str] = "package"
ERROR_CODE_NO_BOUND_STREAM: Final[str] = "noBoundStream"
ERROR_CODE_REGISTRY_SAVE: Final[str] = "registrySaveFailed"
ERROR_CODE_KEYS_WRITE: Final[str] = "keysWriteFailed"


class ExitCode(IntEnum):
    OK = 0            # всё, что можно было сделать, сделано
    ERRORS = 1        # есть ошибки
    CONFIG = 2        # ошибка конфигурации/авторизации/журнала — ничего не делалось
    PROMO_EMPTY = 3   # в promo\ нет пакетов


class RunProblem(str, Enum):
    PROMO_EMPTY = "promo_empty"
    REGISTRY_UNREADABLE = "registry_unreadable"


@dataclass(frozen=True)
class RunOutcome:
    report: RunReport | None
    exit_code: int
    report_text: str | None = None
    report_path: Path | None = None
    problem: RunProblem | None = None
    problem_detail: str = ""


@dataclass(frozen=True)
class _RunContext:
    mode: RunMode
    config: PlanerConfig
    paths: PlanerPaths
    platform: BroadcastPlatform
    form_sender: FormSender
    now_utc: datetime
    rng: random.Random
    notice: str | None
    registry: Registry

    @property
    def now_local(self) -> datetime:
        return self.now_utc.astimezone()

    @property
    def now_naive(self) -> datetime:
        """Для журнала: местное время без смещения (так он читается обратно, §5.4)."""
        return self.now_local.replace(tzinfo=None)

    @property
    def generated_at_text(self) -> str:
        return format_datetime_text(self.now_local)


def run(
    mode: RunMode,
    config: PlanerConfig,
    paths: PlanerPaths,
    platform: BroadcastPlatform,
    form_sender: FormSender,
    now_utc: datetime,
    rng: random.Random,
    *,
    notice: str | None = None,
) -> RunOutcome:
    try:
        registry: Registry = Registry.load(paths.registry_file)
    except RegistryError as error:
        LOGGER.error("registry_unreadable path=%s reason=%s", paths.registry_file, error)
        return RunOutcome(
            report=None,
            exit_code=int(ExitCode.CONFIG),
            problem=RunProblem.REGISTRY_UNREADABLE,
            problem_detail=str(error),
        )
    context: _RunContext = _RunContext(mode, config, paths, platform, form_sender, now_utc, rng, notice, registry)
    if mode is RunMode.STATUS:
        return _run_status(context)
    return _run_promo(context)


def _run_promo(context: _RunContext) -> RunOutcome:
    scan: PromoScan = scan_promo(context.paths, context.now_utc)
    if scan.is_empty:
        return RunOutcome(report=None, exit_code=int(ExitCode.PROMO_EMPTY), problem=RunProblem.PROMO_EMPTY)
    selection: Selection = select_pairs(scan.slot_map, context.config, context.now_utc)
    reconciliation: Reconciliation = Reconciler(context.platform).reconcile(
        selection.pairs,
        context.registry,
        frozenset(scan.slot_map),
        context.config.channels,
    )
    if context.mode is RunMode.FULL:
        outcomes, keys_path = _execute_full(context, scan, reconciliation)
    else:
        outcomes, keys_path = [_planned_outcome(item) for item in reconciliation.pairs], None
    report: RunReport = RunReport(
        mode=context.mode,
        generated_at_text=context.generated_at_text,
        owner=context.config.owner,
        packages=build_package_lines(scan, context.config),
        outcomes=outcomes,
        orphans=[_orphan_line(orphan) for orphan in reconciliation.orphans],
        skipped=build_skipped_lines(scan, selection, context.config),
        keys_file_path=_display_path(context.paths, keys_path),
        notice=context.notice,
    )
    has_errors: bool = _has_error_outcomes(outcomes) or bool(scan.problems)
    return _complete(context, report, has_errors=has_errors)


def _execute_full(
    context: _RunContext,
    scan: PromoScan,
    reconciliation: Reconciliation,
) -> tuple[list[PairOutcome], Path | None]:
    """Пакеты остаются в promo как есть: планер их только читает (§7.1)."""
    outcomes: list[PairOutcome] = _Executor(context, scan).execute(reconciliation.pairs)
    for item in scan.packages:
        context.registry.note_package(item.package.package_id, item.package.path.name, context.now_naive)
    outcomes.extend(_save_registry(context))
    keys_path, keys_errors = _write_keys(context, _registry_key_rows(context, scan))
    outcomes.extend(keys_errors)
    return outcomes, keys_path


def _run_status(context: _RunContext) -> RunOutcome:
    """Без promo: эфиры с маркером планера на каналах → keys.txt и отчёт; журнал не пишется."""
    marked: MarkedScan = Reconciler(context.platform).marked_broadcasts(context.config.channels)
    rows: list[KeyRow] = []
    outcomes: list[PairOutcome] = []
    for item in marked.broadcasts:
        url: str = broadcast_url_for(item.channel, item.broadcast.broadcast_id)
        registration: Registration | None = context.registry.get(Registry.key(item.stream.title, item.channel.id))
        rows.append(
            key_row_from_platform(
                language=item.parts.language,
                date_text=item.parts.date,
                time_text=item.parts.time,
                account_name=item.channel.account_name,
                stream=item.stream,
                broadcast_url=url,
                registration=registration,
            )
        )
        outcomes.append(
            PairOutcome(
                kind=OutcomeKind.MATCHED,
                account_name=item.channel.account_name,
                date=item.parts.date,
                time=item.parts.time,
                language=item.parts.language,
                broadcast_url=url,
            )
        )
    outcomes.extend(
        PairOutcome(kind=OutcomeKind.ERROR, account_name=failure.channel.account_name, error=_platform_error(failure.channel, failure.error))
        for failure in marked.failures
    )
    keys_path, keys_errors = _write_keys(context, rows)
    outcomes.extend(keys_errors)
    report: RunReport = RunReport(
        mode=RunMode.STATUS,
        generated_at_text=context.generated_at_text,
        owner=context.config.owner,
        outcomes=outcomes,
        keys_file_path=_display_path(context.paths, keys_path),
        notice=context.notice,
    )
    return _complete(context, report, has_errors=_has_error_outcomes(outcomes))


def _complete(context: _RunContext, report: RunReport, *, has_errors: bool) -> RunOutcome:
    text: str = render_report(report)
    if context.mode is not RunMode.DRY_RUN:
        # Сначала чистка, потом отчёт: свой же отчёт под неё не попадает (§5.7).
        cleanup_expired(context.paths, context.config.keep_days, context.now_utc)
    report_path: Path = write_report(context.paths, text, context.now_local)
    exit_code: ExitCode = ExitCode.ERRORS if has_errors else ExitCode.OK
    LOGGER.info("run_report mode=%s outcomes=%d exit_code=%d", report.mode.value, len(report.outcomes), int(exit_code))
    return RunOutcome(report=report, exit_code=int(exit_code), report_text=text, report_path=report_path)


def _has_error_outcomes(outcomes: Sequence[PairOutcome]) -> bool:
    return any(outcome.kind in ERROR_OUTCOME_KINDS for outcome in outcomes)


def _display_path(paths: PlanerPaths, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


def _platform_error(channel: ChannelConfig, error: PlatformError | None) -> OutcomeError:
    if error is None:
        return OutcomeError(origin=channel.platform.value, code="unknown")
    return OutcomeError(origin=channel.platform.value, code=error.code, message=error.message)


def _pair_outcome(
    item: ReconciledPair,
    kind: OutcomeKind,
    *,
    broadcast_url: str | None = None,
    form: FormState | None = None,
    recreated: bool = False,
    rebind: bool = False,
    error: OutcomeError | None = None,
) -> PairOutcome:
    slot: Slot = item.pair.slot
    return PairOutcome(
        kind=kind,
        account_name=item.pair.channel.account_name,
        date=slot.date,
        time=slot.time,
        language=slot.language,
        broadcast_url=broadcast_url,
        changed_fields=tuple(changed.value for changed in item.changed_fields),
        form=form,
        recreated=recreated,
        rebind=rebind,
        error=error,
    )


def _found_url(item: ReconciledPair) -> str | None:
    if item.broadcast is None:
        return None
    return broadcast_url_for(item.pair.channel, item.broadcast.broadcast_id)


def _planned_outcome(item: ReconciledPair) -> PairOutcome:
    """Исход без действий: dry-run, а также AMBIGUOUS и ERROR в полном запуске."""
    decision: Decision = item.decision
    if decision is Decision.CREATE:
        return _pair_outcome(item, OutcomeKind.CREATED)
    if decision is Decision.RECREATE:
        return _pair_outcome(item, OutcomeKind.CREATED, recreated=True)
    if decision is Decision.UPDATE:
        return _pair_outcome(item, OutcomeKind.FIXED, broadcast_url=_found_url(item), rebind=item.rebind)
    if decision is Decision.MATCH:
        return _pair_outcome(item, OutcomeKind.MATCHED, broadcast_url=_found_url(item), rebind=item.rebind)
    if decision is Decision.AMBIGUOUS:
        return _pair_outcome(item, OutcomeKind.AMBIGUOUS)
    return _pair_outcome(item, OutcomeKind.ERROR, error=_platform_error(item.pair.channel, item.error))


def _orphan_line(orphan: OrphanBroadcast) -> OrphanLine:
    parts = split_marker(orphan.marker)
    return OrphanLine(
        date=parts.date if parts else orphan.marker,
        time=parts.time if parts else "",
        language=parts.language if parts else "",
        account_name=orphan.channel.account_name,
        broadcast_url=broadcast_url_for(orphan.channel, orphan.broadcast.broadcast_id),
    )


def _planer_error_outcome(account_name: str, code: str, error: Exception) -> PairOutcome:
    return PairOutcome(
        kind=OutcomeKind.ERROR,
        account_name=account_name,
        error=OutcomeError(origin=ERROR_ORIGIN_PLANER, code=code, message=str(error)),
    )


def _save_registry(context: _RunContext) -> list[PairOutcome]:
    try:
        context.registry.save(context.paths.registry_file)
    except OSError as error:
        LOGGER.error("registry_save_failed path=%s reason=%s", context.paths.registry_file, error)
        return [_planer_error_outcome(context.paths.registry_file.name, ERROR_CODE_REGISTRY_SAVE, error)]
    return []


def _write_keys(context: _RunContext, rows: list[KeyRow]) -> tuple[Path | None, list[PairOutcome]]:
    try:
        return write_keys_file(context.paths, render_keys_file(rows, context.generated_at_text)), []
    except OSError as error:
        LOGGER.error("keys_write_failed path=%s reason=%s", context.paths.keys_file, error)
        return None, [_planer_error_outcome(context.paths.keys_file.name, ERROR_CODE_KEYS_WRITE, error)]


def _registry_key_rows(context: _RunContext, scan: PromoScan) -> list[KeyRow]:
    """Все будущие слоты каналов владельца, для которых в журнале есть ключ (§5.5), включая too_late."""
    rows: list[KeyRow] = []
    for slot in scan.slot_map.values():
        for channel in context.config.channels:
            if slot.language not in channel.languages:
                continue
            registration: Registration | None = context.registry.get(Registry.key(slot.slot_id, channel.id))
            if registration is not None and registration.stream_key:
                rows.append(key_row_from_registration(registration))
    return rows


class _Executor:
    """Действия полного запуска по решениям сверки (§7.3, §7.4, §7.5); сбой пары изолирован."""

    def __init__(self, context: _RunContext, scan: PromoScan) -> None:
        self._context: _RunContext = context
        self._scan: PromoScan = scan
        self._registry: Registry = context.registry
        self._platform: BroadcastPlatform = context.platform

    def execute(self, pairs: Sequence[ReconciledPair]) -> list[PairOutcome]:
        return [self._execute_one(item) for item in pairs]

    def _execute_one(self, item: ReconciledPair) -> PairOutcome:
        try:
            return self._dispatch(item)
        except PlatformError as error:
            LOGGER.warning("pair_failed slot_id=%s channel=%s code=%s", item.pair.slot.slot_id, item.pair.channel.id, error.code)
            return _pair_outcome(item, OutcomeKind.ERROR, error=_platform_error(item.pair.channel, error))
        except RegistryError as error:
            LOGGER.error("pair_registry_failed slot_id=%s reason=%s", item.pair.slot.slot_id, error)
            return _pair_outcome(item, OutcomeKind.ERROR, error=OutcomeError(ERROR_ORIGIN_REGISTRY, type(error).__name__, str(error)))
        except PackageError as error:
            LOGGER.error("pair_package_failed slot_id=%s reason=%s", item.pair.slot.slot_id, error)
            return _pair_outcome(item, OutcomeKind.ERROR, error=OutcomeError(ERROR_ORIGIN_PACKAGE, error.reason.value, error.detail))

    def _dispatch(self, item: ReconciledPair) -> PairOutcome:
        decision: Decision = item.decision
        if decision in (Decision.CREATE, Decision.RECREATE):
            return self._create(item, is_recreate=decision is Decision.RECREATE)
        if decision is Decision.UPDATE:
            self._update(item)
            return self._confirmed(item, OutcomeKind.FIXED)
        if decision is Decision.MATCH:
            return self._confirmed(item, OutcomeKind.MATCHED)
        return _planned_outcome(item)

    def _create(self, item: ReconciledPair, *, is_recreate: bool) -> PairOutcome:
        slot, channel = item.pair.slot, item.pair.channel
        created = self._platform.create_broadcast(channel, slot, self._preview(slot, channel))
        LOGGER.info(
            "broadcast_created slot_id=%s channel=%s broadcast_id=%s stream_key=%s",
            slot.slot_id,
            channel.id,
            created.broadcast_id,
            mask_stream_key(created.stream_key),
        )
        key: str = Registry.key(slot.slot_id, channel.id)
        if is_recreate:
            self._registry.mark_recreated(
                key,
                broadcast_id=created.broadcast_id,
                broadcast_url=created.broadcast_url,
                stream_url=created.stream_url,
                stream_key=created.stream_key,
                created_at=self._context.now_naive,
            )
        else:
            self._registry.upsert(
                self._new_registration(
                    slot,
                    channel,
                    broadcast_id=created.broadcast_id,
                    broadcast_url=created.broadcast_url,
                    stream_url=created.stream_url,
                    stream_key=created.stream_key,
                )
            )
        form: FormState = self._send_form(key, slot, channel)
        return _pair_outcome(item, OutcomeKind.CREATED, broadcast_url=created.broadcast_url, form=form, recreated=is_recreate)

    def _update(self, item: ReconciledPair) -> None:
        slot, channel = item.pair.slot, item.pair.channel
        broadcast_id: str = item.broadcast.broadcast_id if item.broadcast else ""
        self._platform.update_broadcast(channel, broadcast_id, slot, self._preview(slot, channel))
        LOGGER.info("broadcast_updated slot_id=%s channel=%s broadcast_id=%s", slot.slot_id, channel.id, broadcast_id)

    def _confirmed(self, item: ReconciledPair, kind: OutcomeKind) -> PairOutcome:
        """Эфир подтверждён на площадке: rebind → журнал на найденный эфир и форма; pending → форма повторно."""
        if item.rebind:
            return self._rebind(item, kind)
        slot, channel = item.pair.slot, item.pair.channel
        key: str = Registry.key(slot.slot_id, channel.id)
        registration: Registration | None = self._registry.get(key)
        form: FormState | None = None
        if registration is not None and registration.form_status is FormStatus.PENDING:
            form = self._send_form(key, slot, channel)
        url: str | None = registration.broadcast_url if registration and registration.broadcast_url else _found_url(item)
        return _pair_outcome(item, kind, broadcast_url=url, form=form)

    def _rebind(self, item: ReconciledPair, kind: OutcomeKind) -> PairOutcome:
        """Эфира нет в журнале (или там другой): ключ — с площадки, в форму как новый (§7.5)."""
        slot, channel, stream = item.pair.slot, item.pair.channel, item.stream
        if stream is None or item.broadcast is None:
            error: OutcomeError = OutcomeError(origin=ERROR_ORIGIN_PLANER, code=ERROR_CODE_NO_BOUND_STREAM)
            return _pair_outcome(item, OutcomeKind.ERROR, error=error)
        key: str = Registry.key(slot.slot_id, channel.id)
        url: str = broadcast_url_for(channel, item.broadcast.broadcast_id)
        if self._registry.get(key) is None:
            self._registry.upsert(
                self._new_registration(
                    slot,
                    channel,
                    broadcast_id=item.broadcast.broadcast_id,
                    broadcast_url=url,
                    stream_url=stream.ingestion_address,
                    stream_key=stream.stream_name,
                )
            )
        else:
            self._registry.mark_recreated(
                key,
                broadcast_id=item.broadcast.broadcast_id,
                broadcast_url=url,
                stream_url=stream.ingestion_address,
                stream_key=stream.stream_name,
                created_at=self._context.now_naive,
            )
        LOGGER.info(
            "registration_rebound slot_id=%s channel=%s broadcast_id=%s stream_key=%s",
            slot.slot_id,
            channel.id,
            item.broadcast.broadcast_id,
            mask_stream_key(stream.stream_name),
        )
        return _pair_outcome(item, kind, broadcast_url=url, form=self._send_form(key, slot, channel), rebind=True)

    def _new_registration(
        self,
        slot: Slot,
        channel: ChannelConfig,
        *,
        broadcast_id: str,
        broadcast_url: str,
        stream_url: str,
        stream_key: str,
    ) -> Registration:
        return Registration(
            slot_id=slot.slot_id,
            channel_id=channel.id,
            account_name=channel.account_name,
            language=slot.language,
            date=slot.date,
            time=slot.time,
            broadcast_id=broadcast_id,
            broadcast_url=broadcast_url,
            stream_url=stream_url,
            stream_key=stream_key,
            package_id=self._source(slot).package_id,
            created_at=self._context.now_naive,
            form_status=FormStatus.PENDING,
            form_sent_at=None,
            previous_broadcast_ids=[],
            last_error=None,
        )

    def _send_form(self, key: str, slot: Slot, channel: ChannelConfig) -> FormState:
        registration: Registration | None = self._registry.get(key)
        if registration is None:
            raise RegistryError(f"unknown registration key={key}")
        result: FormSendResult = self._context.form_sender.send(registration, slot, channel, slot.form)
        if result.confirmed:
            self._registry.mark_form_sent(key, self._context.now_naive)
            self._set_last_error(key, None)
            return FormState.SENT
        if result.error:
            self._set_last_error(key, result.error)
            return FormState.FAILED
        return FormState.WAITING

    def _set_last_error(self, key: str, error: str | None) -> None:
        registration: Registration | None = self._registry.get(key)
        if registration is not None and registration.last_error != error:
            self._registry.upsert(replace(registration, last_error=error))

    def _preview(self, slot: Slot, channel: ChannelConfig) -> bytes | None:
        """Случайное превью слота (§5.1) — если канал ставит превью и в слоте они есть."""
        if not channel.set_thumbnail or not slot.previews:
            return None
        return read_preview(self._source(slot), self._context.rng.choice(slot.previews))

    def _source(self, slot: Slot) -> Package:
        return self._scan.slot_sources[slot.slot_id]
