"""Оркестрация запуска (ТЗ §4): bcast → формы → объекты → входы → допуск → записи памяти → сверка
→ по объекту: действия, запись, решение о ключе, форма, запись подтверждения → keys.txt → отчёт → чистка памяти.

main.py только разбирает флаги, строит зависимости и печатает результат.
Рабочая единица — PlannedBroadcast: один эфир одного слота на одном канале.
Формы читаются сразу после пакетов, до входов и до обращений к площадке (FormSender.prepare).
Фаза входов (ChannelLogins) — между отбором объектов и сверкой: после неё браузер не открывается.
Допуск — после входов: каждый объект получает объект своего канала и своей формы и сам решает (PlannedBroadcast.admit).
Не допущенный объект на площадке ничего не создаёт и в форму ничего не шлёт, но остаётся в отчёте и keys.txt.
Ключ уходит в форму сразу после действий по своему объекту — обрыв запуска не теряет уже созданные ключи (§7.5).
Истина об эфирах и ключах — на площадке. Память планера (app/records) — только о том, что форма уже подтвердила:
из неё объект читает результаты, задания из неё не берутся. Должен ли ключ уйти, решает объект
(PlannedBroadcast.decide_key_delivery); ошибка другого шага отправку уже взятого ключа не отменяет.
Запись в память — только если она изменилась (PlannedBroadcast.record_to_save).
Время старта запуска (now_utc) — для отчёта, имён файлов, отбора too_late и чистки; время событий
(публикация, подтверждение формы, updated_at записи) — из часов (clock) в момент события.
"""
from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Final, Protocol

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.dates import format_datetime_text
from app.core.retention import cleanup_expired
from app.form.base import FormError, FormSender, FormSendResult
from app.form.key_form import FormAnswers, KeyForm
from app.observability.logging_setup import get_logger, mask_stream_key
from app.output.keys_file import (
    KeyRow,
    key_row_from_marked,
    key_row_from_planned,
    render_keys_file,
    write_keys_file,
)
from app.output.progress import BroadcastStep, NoProgress, RunProgress
from app.output.report import (
    ExitReason,
    ExitReasonKind,
    OrphanLine,
    PairOutcome,
    ReportPackageLine,
    RunMode,
    RunExit,
    RunReport,
    RunTotals,
    build_package_lines,
    build_skipped_lines,
    build_mismatch_lines,
    build_totals,
    build_warning_lines,
    display_path,
    outcome_from_marked,
    outcome_from_planned,
    planer_error_outcome,
    platform_error_outcome,
    render_report,
    write_report,
)
from app.package.model import FormSpec, PackageError, read_preview
from app.package.bcast import BcastScan, scan_bcast
from app.paths import PlanerPaths
from app.pipeline.plan import (
    BroadcastSpec,
    SpecValue,
    WARNING_STEP_AGE_RESTRICTED,
    WARNING_STEP_AUDIENCE,
    WARNING_STEP_FACTS,
    WARNING_STEP_SETTINGS,
    WARNING_STEP_THUMBNAIL,
    ChangedField,
    Decision,
    FixCall,
    OutcomeError,
    OutcomeWarning,
    PlannedBroadcast,
)
from app.platforms.channel import Channel
from app.records.record_store import RecordStore
from app.records.slot_record import RecordResults, SlotRecord, SlotStage
from app.ui import messages_ru as msg
from app.pipeline.reconciler import MarkedBroadcast, MarkedScan, OrphanBroadcast, Reconciler, split_marker
from app.pipeline.selection import Selection, build_planned
from app.platforms.base import (
    BroadcastFacts,
    BroadcastPlatform,
    CreatedBroadcast,
    PlatformError,
    PlatformNotice,
    VideoFixes,
    broadcast_url_for,
)

__all__ = ["ExitCode", "RunMode", "RunOutcome", "RunProblem", "decide_exit", "run"]

LOGGER = get_logger("runner")
# OutcomeError.origin для ошибок самого планера и его файлов; тексты — messages_ru.
ERROR_ORIGIN_PACKAGE: Final[str] = "package"
ERROR_CODE_KEYS_WRITE: Final[str] = "keysWriteFailed"
MISSING_FIELD: Final[str] = "-"
DESCRIPTION_HEAD_CHARS: Final[int] = 80
# Ключи строк broadcast_expected и broadcast_found — один набор на обе.
SPEC_LOG_KEYS: Final[tuple[str, ...]] = (
    "start",
    "marker",
    "title",
    "title_len",
    "description_len",
    "description_head",
    "privacy",
    "category_id",
    "auto_start",
    "auto_stop",
    "latency",
    "has_own_thumbnail",
)


class ChannelLogins(Protocol):
    """Фаза входов (app/platforms/channel.py::ChannelBook): каналы без входа входят подряд, один за другим."""

    def log_in_needed(self, channels: Sequence[ChannelConfig]) -> None:
        ...

    def channel(self, config: ChannelConfig) -> Channel:
        """Объект канала после фазы входов: для допуска объектов."""
        ...


class ExitCode(IntEnum):
    OK = 0            # всё, что можно было сделать, сделано
    ERRORS = 1        # есть ошибки
    CONFIG = 2        # ошибка конфигурации/авторизации — ничего не делалось
    BCAST_EMPTY = 3   # в bcast\ нет пакетов


class RunProblem(str, Enum):
    BCAST_EMPTY = "bcast_empty"


@dataclass(frozen=True)
class RunOutcome:
    report: RunReport | None
    exit_code: int
    report_path: Path | None = None
    problem: RunProblem | None = None


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
    form_diagnostics: list[str]   # пути сохранённых ответов формы (§7.5)
    channel_warnings: tuple[str, ...]   # сверка каналов при старте (ChannelSync.run)
    progress: RunProgress
    logins: ChannelLogins | None        # None — входов нет (тесты без ChannelBook)
    store: RecordStore                  # память планера; read_only в --dry-run и --status
    clock: Callable[[], datetime]       # время событий: момент публикации, подтверждения, записи

    @property
    def now_local(self) -> datetime:
        return self.now_utc.astimezone()

    @property
    def generated_at_text(self) -> str:
        return format_datetime_text(self.now_local)

    @property
    def is_full(self) -> bool:
        return self.mode is RunMode.FULL


def utc_now() -> datetime:
    """Часы запуска по умолчанию: текущее время UTC."""
    return datetime.now(timezone.utc)


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
    progress: RunProgress = NoProgress(),
    channel_warnings: Sequence[str] = (),
    logins: ChannelLogins | None = None,
    store: RecordStore | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> RunOutcome:
    """channel_warnings — предупреждения сверки каналов при старте: в отчёт и консоль вместе с прочими.

    logins — фаза входов: каналы с объектами (в --status — все каналы) входят до первого обращения к площадке.
    store — память планера; открывает и закрывает её main. Без неё (тесты) — пустая память в оперативной
    памяти, как у первого запуска.
    clock — часы для времени событий; тесты передают свои.
    """
    context: _RunContext = _RunContext(
        mode, config, paths, platform, form_sender, now_utc, rng, notice, [], tuple(channel_warnings), progress, logins,
        store if store is not None else RecordStore.memory(is_new=True),
        clock,
    )
    if mode is RunMode.STATUS:
        return _run_status(context)
    return _run_bcast(context)


def _run_bcast(context: _RunContext) -> RunOutcome:
    scan: BcastScan = scan_bcast(context.paths, context.now_utc)
    if scan.is_empty:
        return RunOutcome(report=None, exit_code=int(ExitCode.BCAST_EMPTY), problem=RunProblem.BCAST_EMPTY)
    packages: list[ReportPackageLine] = build_package_lines(scan, context.config)
    _progress_packages(context, packages)
    # формы — один раз на форму, до входов и до обращений к площадке
    context.form_sender.prepare([slot.form for slot in scan.slot_map.values()])
    selection: Selection = build_planned(
        scan.slot_map,
        scan.slot_sources,
        context.config,
        context.platform.limits,
        context.now_utc,
    )
    # к площадке обращаемся только по каналам, у которых есть объекты (и too_late): входы — все до сверки
    _log_in(context, [item.channel for item in selection.planned])
    _admit_all(context, selection.planned)
    _load_records(context, selection.planned)
    # известные слоты — будущие и прошедшие: эфир прошедшего слота, который ещё не начался, не сирота
    known_slot_ids: frozenset[str] = frozenset(scan.slot_map) | frozenset(slot.slot_id for slot in scan.past_slots)
    orphans: tuple[OrphanBroadcast, ...] = Reconciler(context.platform, progress=context.progress).reconcile(
        selection.planned,
        known_slot_ids,
    )
    memory_warnings: list[str] = _bootstrap_records(context, selection.planned)
    # замечания площадки (эфир без времени старта) — данными, в отчёт и консоль одним путём
    notices: tuple[PlatformNotice, ...] = context.platform.take_notices()
    keys_path: Path | None = None
    extra_outcomes: list[PairOutcome] = []
    if context.is_full:
        keys_path, extra_outcomes = _execute_full(context, selection)
    else:
        _decide_without_actions(selection.planned)
    outcomes: list[PairOutcome] = [
        outcome_from_planned(item, is_dry_run=not context.is_full)
        for item in selection.planned
        if not item.is_too_late      # они в разделе «пропущено», не в исходах
    ]
    outcomes.extend(_channel_refusal_outcomes(selection.planned))
    outcomes.extend(extra_outcomes)
    report: RunReport = RunReport(
        mode=context.mode,
        generated_at_text=context.generated_at_text,
        packages=packages,
        outcomes=outcomes,
        orphans=[_orphan_line(orphan) for orphan in orphans],
        skipped=build_skipped_lines(scan, selection, context.config),
        mismatches=build_mismatch_lines(selection.planned, context.platform.limits),
        warnings=build_warning_lines(
            selection.planned,
            context.form_diagnostics,
            notices,
            [*context.channel_warnings, *memory_warnings, *context.store.take_warnings()],
        ),
        keys_file_path=display_path(context.paths.root, keys_path),
        notice=context.notice,
    )
    # ключ, который должен был дойти до стримера и не дошёл, — это код выхода 1 (§7.5)
    undelivered: int = sum(1 for item in selection.planned if item.is_key_undelivered) if context.is_full else 0
    return _complete(context, report, package_problems=len(scan.problems), undelivered=undelivered)


def _log_in(context: _RunContext, channels: Sequence[ChannelConfig]) -> None:
    if context.logins is not None:
        context.logins.log_in_needed(channels)


def _admit_all(context: _RunContext, planned: Sequence[PlannedBroadcast]) -> None:
    """Допуск: у каждого объекта — объект канала и объект формы; решает сам объект (PlannedBroadcast.admit)."""
    for item in planned:
        channel_object: Channel | None = context.logins.channel(item.channel) if context.logins is not None else None
        key_form, failure = _form_for(context, item.form)
        item.admit(channel_object, key_form, failure)
        if not item.is_too_late:
            _log_admission(item)


def _load_records(context: _RunContext, planned: Sequence[PlannedBroadcast]) -> None:
    """Каждому объекту подтверждённого канала — его запись из памяти (обратно — только результаты)."""
    for item in planned:
        channel_id: str | None = item.record_channel_id
        if channel_id is not None:
            item.record = context.store.find(item.slot_id, channel_id)


def _bootstrap_records(context: _RunContext, planned: Sequence[PlannedBroadcast]) -> list[str]:
    """Каждый запуск: эфиры с меткой планера, созданные на площадке раньше памяти и без записи, — уже переданные."""
    count: int = 0
    for item in planned:
        if not item.bootstrap_confirmation(context.clock(), context.store.created_utc):
            continue
        count += 1
        LOGGER.info(
            'record_bootstrap slot_id=%s channel="%s" handle=%s broadcast_id=%s stream_key=%s published_utc=%s '
            "memory_created_utc=%s",
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            item.broadcast_id,
            mask_stream_key(item.stream_key),
            item.found.published_utc.isoformat() if item.found and item.found.published_utc else MISSING_FIELD,
            context.store.created_utc.isoformat(),
        )
        _save_record(context, item, SlotStage.KEY_CONFIRMED)
    if count == 0:
        return []
    return [msg.WARNING_RECORDS_CREATED.format(count=count)]


def _decide_without_actions(planned: Sequence[PlannedBroadcast]) -> None:
    """--dry-run: решение о ключе — по записям памяти, чтобы «отправим ключ» было правдой."""
    for item in planned:
        if item.stream_key:
            item.decide_key_delivery()


def _save_record(context: _RunContext, item: PlannedBroadcast, stage: SlotStage) -> None:
    """Запись объекта, если она изменилась; канал без id YouTube — записи нет.

    Сбой базы — предупреждение хранилища, объект идёт дальше и сравнивает следующую запись с прежней.
    """
    channel_id: str | None = item.record_channel_id
    if channel_id is None:
        return
    record: SlotRecord | None = item.record_to_save(context.clock(), stage)
    if record is None:
        LOGGER.debug(
            "record_unchanged slot_id=%s youtube_channel_id=%s stage=%s",
            item.slot_id,
            channel_id,
            item.record.stage.value if item.record is not None else stage.value,
        )
        return
    if context.store.save(record, requested=stage):
        item.remember_record(record)


def _form_for(context: _RunContext, spec: FormSpec) -> tuple[KeyForm | None, FormError | None]:
    try:
        return context.form_sender.form_for(spec), None
    except FormError as error:
        return None, error


def _log_admission(item: PlannedBroadcast) -> None:
    identity: str = f'slot_id={item.slot_id} channel="{item.channel.account_name}" handle={item.channel.handle}'
    if item.is_admitted:
        LOGGER.info("slot_admitted %s form_url=%s", identity, item.form.url)
        return
    LOGGER.warning(
        "slot_not_admitted %s reasons=%s",
        identity,
        ",".join(f"{reason.kind.value}:{reason.code}:{reason.field or MISSING_FIELD}" for reason in item.admission_reasons),
    )
    for reason in item.admission_reasons:
        LOGGER.warning(
            "slot_not_admitted_reason %s kind=%s code=%s field=%s text=%s",
            identity,
            reason.kind.value,
            reason.code,
            reason.field or MISSING_FIELD,
            _quoted(reason.text),
        )


def _channel_refusal_outcomes(planned: Sequence[PlannedBroadcast]) -> list[PairOutcome]:
    """Полный текст отказа или сбоя канала — один раз на канал; его объекты — в «Не допущено» коротко."""
    outcomes: dict[str, PairOutcome] = {}
    for item in planned:
        channel_object: Channel | None = item.channel_object
        if item.is_admitted or channel_object is None or item.channel.key in outcomes:
            continue
        error: PlatformError | None = channel_object.access_error()
        if error is not None:
            outcomes[item.channel.key] = platform_error_outcome(item.channel, error)
    return list(outcomes.values())


def _progress_packages(context: _RunContext, packages: list[ReportPackageLine]) -> None:
    """Числа — тем же подсчётом, что «Итог» и раздел «Пакеты» отчёта (build_totals), второго счёта нет."""
    totals: RunTotals = build_totals(
        RunReport(mode=context.mode, generated_at_text=context.generated_at_text, packages=packages)
    )
    context.progress.packages_read(totals.packages, totals.slots_total, totals.slots_mine)


def _execute_full(
    context: _RunContext,
    selection: Selection,
) -> tuple[Path | None, list[PairOutcome]]:
    """По объекту: действия → его ключ в форму; затем keys.txt (§4, §7.5)."""
    executor: _Executor = _Executor(context)
    for item in selection.planned:
        if item.is_admitted and not item.is_too_late:
            _save_record(context, item, SlotStage.ADMITTED)
    for item in selection.planned:
        executor.execute(item)
    keys_path, outcomes = _write_keys(
        context,
        # все будущие эфиры с ключом, включая слоты внутри min_lead_minutes и не допущенные (§5.5)
        [key_row_from_planned(item) for item in selection.planned if item.stream_key],
    )
    return keys_path, outcomes


def _run_status(context: _RunContext) -> RunOutcome:
    """Без пакетов: входы всех каналов → эфиры с маркером планера на каналах → keys.txt и отчёт."""
    _log_in(context, context.config.channels)
    marked: MarkedScan = Reconciler(context.platform, progress=context.progress).marked_broadcasts(
        context.config.channels
    )
    notices: tuple[PlatformNotice, ...] = context.platform.take_notices()
    rows: list[KeyRow] = [key_row_from_marked(item, _marked_results(context, item)) for item in marked.broadcasts]
    outcomes: list[PairOutcome] = [outcome_from_marked(item) for item in marked.broadcasts]
    outcomes.extend(platform_error_outcome(failure.channel, failure.error) for failure in marked.failures)
    keys_path, keys_errors = _write_keys(context, rows)
    outcomes.extend(keys_errors)
    report: RunReport = RunReport(
        mode=RunMode.STATUS,
        generated_at_text=context.generated_at_text,
        outcomes=outcomes,
        warnings=build_warning_lines((), (), notices, [*context.channel_warnings, *context.store.take_warnings()]),
        keys_file_path=display_path(context.paths.root, keys_path),
        notice=context.notice,
    )
    return _complete(context, report)


def _marked_results(context: _RunContext, marked: MarkedBroadcast) -> RecordResults | None:
    """--status: подтверждение из памяти для строки «форма» в keys.txt."""
    channel_id: str | None = marked.channel.key
    if context.logins is not None:
        channel: Channel = context.logins.channel(marked.channel)
        channel_id = channel.info.youtube_channel_id if channel.info is not None else None
    if channel_id is None:
        return None
    record: SlotRecord | None = context.store.find(marked.stream.title, channel_id)
    return record.results if record is not None else None


def _complete(
    context: _RunContext,
    report: RunReport,
    *,
    package_problems: int = 0,
    undelivered: int = 0,
) -> RunOutcome:
    run_exit: RunExit = decide_exit(report, package_problems=package_problems, undelivered=undelivered)
    report = replace(report, run_exit=run_exit)
    context.progress.report_started()
    text: str = render_report(report)
    if context.mode is not RunMode.DRY_RUN:
        # Сначала чистка, потом отчёт: свой же отчёт под неё не попадает (§5.7).
        cleanup_expired(context.paths, context.config.settings.keep_days, context.now_utc)
    if context.is_full:
        _clean_records(context)
    report_path: Path = write_report(context.paths, text, context.now_local)
    LOGGER.info(
        "run_report mode=%s outcomes=%d exit_code=%d reason=%s",
        report.mode.value,
        len(report.outcomes),
        run_exit.code,
        run_exit.log_text,
    )
    return RunOutcome(report=report, exit_code=run_exit.code, report_path=report_path)


def decide_exit(report: RunReport, *, package_problems: int = 0, undelivered: int = 0) -> RunExit:
    """Код выхода и его причины — единственное решение; консоль, отчёт и run_report берут их отсюда.

    Код 1: ошибки по эфирам, сбои каналов и файлов, ключ не дошёл до стримера (§7.5),
    не допущенные к публикации объекты, непрочитанные пакеты.
    """
    totals: RunTotals = build_totals(report)
    counts: tuple[tuple[ExitReasonKind, int], ...] = (
        (ExitReasonKind.ERRORS, totals.errors),
        (ExitReasonKind.FAILURES, totals.failures),
        (ExitReasonKind.KEY_UNDELIVERED, undelivered),
        (ExitReasonKind.NOT_ADMITTED, totals.not_admitted),
        (ExitReasonKind.PACKAGES, package_problems),
    )
    reasons: tuple[ExitReason, ...] = tuple(ExitReason(kind, count) for kind, count in counts if count > 0)
    code: ExitCode = ExitCode.ERRORS if reasons else ExitCode.OK
    return RunExit(code=int(code), reasons=reasons)


def _clean_records(context: _RunContext) -> None:
    """Записи слотов старше keep_days — тот же срок, что у файлов в bcast\\ и logs\\."""
    border: datetime = context.now_utc - timedelta(days=context.config.settings.keep_days)
    removed: int = context.store.delete_started_before(border)
    LOGGER.info("records_cleaned removed=%d", removed)


def _form_error_text(result: FormSendResult) -> str | None:
    """Код исхода в объект (last_error): текст для владельца собирает report.py."""
    if not result.code:
        return result.error
    return f"{result.code}: {result.error}" if result.error else result.code


def _orphan_line(orphan: OrphanBroadcast) -> OrphanLine:
    parts = split_marker(orphan.marker)
    return OrphanLine(
        date=parts.date if parts else orphan.marker,
        time=parts.time if parts else "",
        language=parts.language if parts else "",
        account_name=orphan.channel.account_name,
        handle=orphan.channel.handle,
        broadcast_url=broadcast_url_for(orphan.channel, orphan.broadcast.broadcast_id),
    )


def _write_keys(context: _RunContext, rows: list[KeyRow]) -> tuple[Path | None, list[PairOutcome]]:
    try:
        return write_keys_file(context.paths, render_keys_file(rows, context.generated_at_text)), []
    except OSError as error:
        LOGGER.error("keys_write_failed path=%s reason=%s", context.paths.keys_file, error)
        return None, [planer_error_outcome(context.paths.keys_file.name, ERROR_CODE_KEYS_WRITE, str(error))]


def _describe_spec(spec: BroadcastSpec | None) -> dict[str, object]:
    """Набор ключей для broadcast_expected и broadcast_found — считается здесь, в одном месте."""
    if spec is None:
        return {key: MISSING_FIELD for key in SPEC_LOG_KEYS}
    return {
        "start": spec.start_minute.isoformat(),
        "marker": spec.marker or MISSING_FIELD,
        "title": _quoted(spec.title),
        "title_len": len(spec.title),
        "description_len": len(spec.description),
        "description_head": _quoted(spec.description[:DESCRIPTION_HEAD_CHARS]),
        "privacy": _log_value(spec.privacy),
        "category_id": _log_value(spec.category_id),
        "auto_start": _log_value(spec.auto_start),
        "auto_stop": _log_value(spec.auto_stop),
        "latency": _log_value(spec.latency_preference),
        "has_own_thumbnail": _log_value(spec.has_own_thumbnail),
    }


def _log_value(value: SpecValue) -> object:
    return MISSING_FIELD if value is None else value


def _describe_facts(facts: BroadcastFacts) -> dict[str, object]:
    return {
        "privacy": facts.privacy_status or MISSING_FIELD,
        "made_for_kids": facts.made_for_kids,
        "age_restricted": facts.age_restricted,
        "default_language": facts.default_language or MISSING_FIELD,
        "default_audio_language": facts.default_audio_language or MISSING_FIELD,
        "category_id": facts.category_id or MISSING_FIELD,
        "bound_stream_id": facts.bound_stream_id or MISSING_FIELD,
        "stream_marker": facts.stream_marker or MISSING_FIELD,
        "auto_start": _log_value(facts.auto_start),
        "auto_stop": _log_value(facts.auto_stop),
        "latency": _log_value(facts.latency_preference),
    }


def _quoted(text: str) -> str:
    """Значение с пробелами — в кавычках: строка лога должна разбираться как key=value."""
    return '\"' + _one_line(text) + '\"'


def _one_line(text: str) -> str:
    """Переводы строк — в \\n: строка лога должна оставаться одной строкой."""
    return text.replace(chr(13), '').replace(chr(10), '\\n')


def _log_line(item: PlannedBroadcast, fields: dict[str, object]) -> str:
    identity: dict[str, object] = {
        "slot_id": item.slot_id,
        "channel": _quoted(item.channel.account_name),
        "handle": item.channel.handle,
    }
    return " ".join(f"{key}={value}" for key, value in (identity | fields).items())


def _log_broadcast_fields(item: PlannedBroadcast) -> None:
    """Что хотели (expected), каким эфир нашёлся в списке эфиров до правки (found) и что лежит на платформе
    после действий (facts) — для разбора расхождений. found — не состояние после правки: это facts.
    """
    LOGGER.info("broadcast_expected %s", _log_line(item, _describe_spec(item.expected)))
    LOGGER.info("broadcast_found %s", _log_line(item, _describe_spec(item.actual)))
    if item.facts is not None:
        LOGGER.info("broadcast_facts %s", _log_line(item, _describe_facts(item.facts)))


def _required_text(value: str | None, name: str) -> str:
    """Спека из слота заполняет все диктуемые поля; пустое — ошибка построения, а не площадки."""
    if value is None:
        raise ValueError(f"expected spec has no {name}")
    return value


def _mark_video_fixes(item: PlannedBroadcast, fixes: VideoFixes) -> None:
    """Категория и видимость, исправленные у ресурса видео, — это исправление эфира: UPDATE.

    Категорию список эфиров YouTube не возвращает, поэтому её расхождение видно только здесь.
    """
    fixed: list[ChangedField] = []
    if fixes.category_set:
        fixed.append(ChangedField.CATEGORY)
    if fixes.privacy_set:
        fixed.append(ChangedField.PRIVACY)
    item.mark_fixed(tuple(fixed))
    added: list[ChangedField] = [name for name in fixed if name not in item.changed_fields]
    if not added:
        return
    item.changed_fields = tuple(name for name in ChangedField if name in (*item.changed_fields, *added))
    if item.decision is Decision.MATCH:
        item.decision = Decision.UPDATE
    LOGGER.info(
        'video_fields_fixed slot_id=%s channel="%s" handle=%s fields=%s',
        item.slot_id,
        item.channel.account_name,
        item.channel.handle,
        ",".join(name.value for name in added),
    )


class _Executor:
    """Действия полного запуска по решениям сверки (§7.3, §7.4); сбой объекта изолирован."""

    def __init__(self, context: _RunContext) -> None:
        self._context: _RunContext = context
        self._platform: BroadcastPlatform = context.platform
        self._resent: set[tuple[str, str]] = set()   # (slot_id, channel.key): эфир уже переотправлен в этом запуске

    def execute(self, item: PlannedBroadcast) -> None:
        """Действия по объекту и сразу его ключ: ошибка шага отправку уже взятого ключа не отменяет."""
        try:
            self._dispatch(item)
            self._finish(item)
        except PlatformError as error:
            LOGGER.warning('pair_failed slot_id=%s channel="%s" handle=%s code=%s', item.slot_id, item.channel.account_name, item.channel.handle, error.code)
            item.error = OutcomeError(origin=item.channel.platform.value, code=error.code, message=error.message)
            item.last_error = error.message or error.code
            item.decision = Decision.ERROR
        except PackageError as error:
            LOGGER.error("pair_package_failed slot_id=%s reason=%s", item.slot_id, error)
            item.error = OutcomeError(origin=ERROR_ORIGIN_PACKAGE, code=error.reason.value, message=error.detail)
            item.last_error = error.detail
            item.decision = Decision.ERROR
        self._deliver_key(item)

    def _deliver_key(self, item: PlannedBroadcast) -> None:
        """Ключ взят с площадки: записать → объект решает → в форму → записать подтверждение."""
        if not item.stream_key or not item.is_admitted or item.is_too_late:
            return
        _save_record(self._context, item, SlotStage.PUBLISHED)
        item.decide_key_delivery()
        if not item.should_send_key:
            return
        confirmed_at: datetime | None = self._send_key(item)
        if confirmed_at is not None:
            item.confirm_key(confirmed_at)
            _save_record(self._context, item, SlotStage.KEY_CONFIRMED)

    def _send_key(self, item: PlannedBroadcast) -> datetime | None:
        """Ключ этого объекта — в форму сейчас (should_send_key уже решён объектом, §7.5).

        Ответы достроены объектом (ключ и адрес потока); неполные — без POST, причина в last_error.
        Возвращает момент, когда форма подтвердила (часы запуска); не подтвердила — None.
        """
        answers: FormAnswers | None = item.form_answers
        if not item.is_key_ready_to_send:
            error: FormError | None = answers.error() if answers is not None else None
            if error is not None:
                item.last_error = f"{error.code}: {error.message}"
                LOGGER.warning(
                    'form_send_skipped slot_id=%s channel="%s" handle=%s code=%s text=%s',
                    item.slot_id,
                    item.channel.account_name,
                    item.channel.handle,
                    error.code,
                    _quoted(error.message),
                )
            return None
        self._context.progress.key_send_started(item)
        result: FormSendResult = self._context.form_sender.send(item)
        confirmed_at: datetime | None = None
        if result.confirmed:
            confirmed_at = self._context.clock()
            item.is_form_sent = True
            item.form_sent_at = confirmed_at.astimezone().replace(tzinfo=None)   # местное время без смещения
            item.last_error = None
        else:
            item.last_error = _form_error_text(result)
        if result.diagnostic_path is not None:
            self._context.form_diagnostics.append(str(result.diagnostic_path))
        LOGGER.info(
            'form_send slot_id=%s channel="%s" handle=%s confirmed=%s stream_key=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            result.confirmed,
            mask_stream_key(item.stream_key),
        )
        return confirmed_at

    def _dispatch(self, item: PlannedBroadcast) -> None:
        if item.is_too_late or not item.is_admitted:
            return      # too_late: ключ храним, эфир не трогаем; не допущен: на площадке ничего не делаем
        if item.decision is Decision.NO_STREAM:
            self._attach_stream(item)
            return
        if item.decision is Decision.CREATE:
            self._create(item)
            return
        if item.decision is Decision.UPDATE:
            self._fix(item)
        # MATCH: ключ и ссылку сверка уже взяла с площадки, действий нет; уйдёт ли ключ — решает объект

    def _create(self, item: PlannedBroadcast) -> None:
        self._context.progress.broadcast_step_started(item, BroadcastStep.CREATE)
        created: CreatedBroadcast = self._platform.create_broadcast(item.channel, item.expected)
        item.take_new_key(created)
        LOGGER.info(
            'broadcast_created slot_id=%s channel="%s" handle=%s broadcast_id=%s stream_key=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            created.broadcast_id,
            mask_stream_key(created.stream_key),
        )
        self._set_thumbnail(item, created.broadcast_id)

    def _attach_stream(self, item: PlannedBroadcast) -> None:
        """Эфир есть, потока нет: привязываем поток и дальше ведём себя как с найденным."""
        if item.found is None:
            return
        attached: CreatedBroadcast = self._platform.attach_stream(
            item.channel,
            item.found.broadcast_id,
            item.expected,
        )
        item.stream_attached = True
        item.take_new_key(attached)
        LOGGER.info(
            'stream_attached slot_id=%s channel="%s" handle=%s broadcast_id=%s stream_key=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            attached.broadcast_id,
            mask_stream_key(attached.stream_key),
        )
        # исправимые поля сверка уже посчитала (метку новый поток получил при создании)
        item.decision = Decision.UPDATE if item.changed_fields else Decision.MATCH
        if item.changed_fields:
            self._fix(item)

    def _fix(self, item: PlannedBroadcast) -> None:
        broadcast_id: str = item.found.broadcast_id if item.found else ""
        self._resend(item, broadcast_id, with_marker=True)

    def _resend(self, item: PlannedBroadcast, broadcast_id: str, *, with_marker: bool) -> None:
        """Одна правка на эфир за запуск: только вызовы, которых требуют расхождения (FIX_CALLS).

        Поля видео (видимость, категория) правит проход настроек видео в _finish.
        """
        self._resent.add((item.slot_id, item.channel.key))
        self._context.progress.broadcast_step_started(item, BroadcastStep.FIX)
        calls: frozenset[FixCall] = item.fix_calls
        if FixCall.BROADCAST in calls:
            self._platform.update_broadcast(item.channel, broadcast_id, item.expected)
            item.mark_fixed(item.fields_fixed_by(FixCall.BROADCAST))
        if with_marker and FixCall.STREAM in calls and item.found_stream is not None:
            # ручной эфир усыновлён: со следующего запуска видно, что ключ уходил стримеру
            self._platform.set_stream_marker(item.channel, item.found_stream.stream_id, item.expected.marker)
            item.mark_fixed(item.fields_fixed_by(FixCall.STREAM))
        if FixCall.THUMBNAIL in calls:
            self._set_thumbnail(item, broadcast_id)
        LOGGER.info(
            'broadcast_updated slot_id=%s channel="%s" handle=%s broadcast_id=%s fields=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            broadcast_id,
            ",".join(changed.value for changed in item.changed_fields),
        )

    def _finish(self, item: PlannedBroadcast) -> None:
        """Аудитория и снимок фактов — по каждому эфиру, который планер считает своим."""
        broadcast_id: str | None = self._own_broadcast_id(item)
        if broadcast_id is None:
            return
        was_match: bool = item.decision is Decision.MATCH
        fixes: VideoFixes | None = self._apply_video_settings(item, broadcast_id)
        is_resent: bool = (item.slot_id, item.channel.key) in self._resent
        if was_match and item.decision is Decision.UPDATE and not is_resent:
            # видимость или категория разошлись у ресурса видео: эфир тоже переотправляется целиком,
            # настройки видео второй раз не проходятся
            self._resend(item, broadcast_id, with_marker=False)
        self._read_facts(item, broadcast_id, fixes)

    @staticmethod
    def _own_broadcast_id(item: PlannedBroadcast) -> str | None:
        """Эфир планера: создан, привязан, исправлен или подтверждён. Прочие — не наше дело."""
        if item.error is not None or item.is_too_late or not item.is_admitted:
            return None
        if item.decision not in (Decision.CREATE, Decision.UPDATE, Decision.MATCH):
            return None
        return item.broadcast_id or (item.found.broadcast_id if item.found else None)

    def _apply_video_settings(self, item: PlannedBroadcast, broadcast_id: str) -> VideoFixes | None:
        """Язык, категория, видимость и аудитория — одним проходом по ресурсу видео (§7.4).

        Возвращает ответ площадки на запись: снимок фактов сверяется с ним, а не с перечитыванием.
        None — записи не было, потому что вызов не удался.
        """
        try:
            fixes: VideoFixes = self._platform.apply_video_settings(
                item.channel,
                broadcast_id,
                item.language,
                _required_text(item.expected.category_id, "category_id"),
                _required_text(item.expected.privacy, "privacy"),
            )
        except PlatformError as error:
            LOGGER.warning(
                'video_settings_failed slot_id=%s channel="%s" handle=%s code=%s',
                item.slot_id,
                item.channel.account_name,
                item.channel.handle,
                error.code,
            )
            item.warn(OutcomeWarning(WARNING_STEP_SETTINGS, error.code, error.message))
            return None
        if fixes.audience_cleared:
            item.warn(OutcomeWarning(WARNING_STEP_AUDIENCE, "fixed"))
        if item.found is not None:
            # у созданного эфира категорию и видимость ставит планер; у найденного это исправление
            _mark_video_fixes(item, fixes)
        return fixes

    def _read_facts(self, item: PlannedBroadcast, broadcast_id: str, fixes: VideoFixes | None = None) -> None:
        """Один раз на объект: что по факту лежит на платформе (§5.6).

        Поля, записанные этим же запуском, берутся из ответа записи (VideoFixes.apply_to_facts):
        перечитывание сразу после неё может отдать ещё старое значение.
        """
        try:
            facts: BroadcastFacts = self._platform.read_facts(item.channel, broadcast_id)
        except PlatformError as error:
            LOGGER.warning(
                'facts_read_failed slot_id=%s channel="%s" handle=%s code=%s',
                item.slot_id,
                item.channel.account_name,
                item.channel.handle,
                error.code,
            )
            item.warn(OutcomeWarning(WARNING_STEP_FACTS, error.code, error.message))
            return
        item.facts = facts if fixes is None else fixes.apply_to_facts(facts)
        _log_broadcast_fields(item)
        if item.facts.age_restricted:
            item.warn(OutcomeWarning(WARNING_STEP_AGE_RESTRICTED, "ytAgeRestricted", item.broadcast_url or ""))

    def _set_thumbnail(self, item: PlannedBroadcast, broadcast_id: str) -> None:
        """Превью не критично (ТЗ §7.4 п.4): эфир и ключ остаются в силе.

        Поставлена — факт в объект (память); канал уже отказал в загрузке обложек — без обращения.
        """
        preview: bytes | None = self._preview(item)
        if preview is None:
            return
        refusal: PlatformError | None = self._platform.thumbnail_refusal(item.channel)
        if refusal is not None:
            self._thumbnail_failed(item, refusal)
            return
        try:
            self._platform.set_thumbnail(item.channel, broadcast_id, preview)
        except PlatformError as error:
            self._thumbnail_failed(item, error)
            return
        item.remember_thumbnail(broadcast_id, self._context.clock())
        item.mark_fixed(item.fields_fixed_by(FixCall.THUMBNAIL))

    @staticmethod
    def _thumbnail_failed(item: PlannedBroadcast, error: PlatformError) -> None:
        LOGGER.warning(
            'thumbnail_failed slot_id=%s channel="%s" handle=%s code=%s',
            item.slot_id,
            item.channel.account_name,
            item.channel.handle,
            error.code,
        )
        item.warn(OutcomeWarning(WARNING_STEP_THUMBNAIL, error.code, error.message))
        for name in item.fields_fixed_by(FixCall.THUMBNAIL):
            item.mark_unfixed(name)

    def _preview(self, item: PlannedBroadcast) -> bytes | None:
        """Случайное превью слота (§5.1) — если planer.json велит ставить превью и в слоте они есть."""
        if not self._context.config.settings.set_thumbnail or not item.slot.previews:
            return None
        return read_preview(item.source_package, self._context.rng.choice(item.slot.previews))
