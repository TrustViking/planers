"""Оркестрация запуска (ТЗ §4): promo → объекты → сверка → действия → форма → keys.txt → отчёт.

main.py только разбирает флаги, строит зависимости и печатает результат.
Рабочая единица — PlannedBroadcast: один эфир одного слота на одном канале.
Отправка в форму — отдельным финальным проходом, только новые ключи этого запуска (§7.5).
Истина об эфирах — на площадке: планер не держит своей памяти о прошлых запусках.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import Final

from app.config.loader import PlanerConfig
from app.core.dates import format_datetime_text
from app.core.retention import cleanup_expired
from app.form.base import FormSender, FormSendResult
from app.observability.logging_setup import get_logger, mask_stream_key
from app.output.keys_file import (
    KeyRow,
    key_row_from_marked,
    key_row_from_planned,
    render_keys_file,
    write_keys_file,
)
from app.output.report import (
    ERROR_OUTCOME_KINDS,
    OrphanLine,
    PairOutcome,
    RunMode,
    RunReport,
    build_package_lines,
    build_skipped_lines,
    build_mismatch_lines,
    build_warning_lines,
    display_path,
    outcome_from_marked,
    outcome_from_planned,
    planer_error_outcome,
    platform_error_outcome,
    render_report,
    write_report,
)
from app.package.model import PackageError, read_preview
from app.package.promo import PromoScan, scan_promo
from app.paths import PlanerPaths
from app.pipeline.plan import (
    BroadcastSpec,
    WARNING_STEP_AGE_RESTRICTED,
    WARNING_STEP_AUDIENCE,
    WARNING_STEP_CATEGORY,
    WARNING_STEP_FACTS,
    WARNING_STEP_SETTINGS,
    WARNING_STEP_THUMBNAIL,
    ChangedField,
    Decision,
    OutcomeError,
    OutcomeWarning,
    PlannedBroadcast,
)
from app.pipeline.reconciler import MarkedScan, OrphanBroadcast, Reconciler, split_marker
from app.pipeline.selection import Selection, build_planned
from app.platforms.base import (
    BroadcastFacts,
    BroadcastPlatform,
    CreatedBroadcast,
    PlatformError,
    VideoFixes,
    broadcast_url_for,
)

__all__ = ["ExitCode", "RunMode", "RunOutcome", "RunProblem", "run"]

LOGGER = get_logger("runner")
# OutcomeError.origin для ошибок самого планера и его файлов; тексты — messages_ru.
ERROR_ORIGIN_PACKAGE: Final[str] = "package"
ERROR_CODE_KEYS_WRITE: Final[str] = "keysWriteFailed"
MISSING_FIELD: Final[str] = "-"
DESCRIPTION_HEAD_CHARS: Final[int] = 80
# Ключи строк broadcast_expected и broadcast_actual — один набор на обе.
SPEC_LOG_KEYS: Final[tuple[str, ...]] = (
    "start",
    "marker",
    "title",
    "title_len",
    "description_len",
    "description_head",
)


class ExitCode(IntEnum):
    OK = 0            # всё, что можно было сделать, сделано
    ERRORS = 1        # есть ошибки
    CONFIG = 2        # ошибка конфигурации/авторизации — ничего не делалось
    PROMO_EMPTY = 3   # в promo\ нет пакетов


class RunProblem(str, Enum):
    PROMO_EMPTY = "promo_empty"


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

    @property
    def now_local(self) -> datetime:
        return self.now_utc.astimezone()

    @property
    def now_naive(self) -> datetime:
        """Для form_sent_at: местное время без смещения."""
        return self.now_local.replace(tzinfo=None)

    @property
    def generated_at_text(self) -> str:
        return format_datetime_text(self.now_local)

    @property
    def is_full(self) -> bool:
        return self.mode is RunMode.FULL


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
    context: _RunContext = _RunContext(mode, config, paths, platform, form_sender, now_utc, rng, notice, [])
    if mode is RunMode.STATUS:
        return _run_status(context)
    return _run_promo(context)


def _run_promo(context: _RunContext) -> RunOutcome:
    scan: PromoScan = scan_promo(context.paths, context.now_utc)
    if scan.is_empty:
        return RunOutcome(report=None, exit_code=int(ExitCode.PROMO_EMPTY), problem=RunProblem.PROMO_EMPTY)
    selection: Selection = build_planned(
        scan.slot_map,
        scan.slot_sources,
        context.config,
        context.platform.limits,
        context.now_utc,
    )
    orphans: tuple[OrphanBroadcast, ...] = Reconciler(context.platform).reconcile(
        selection.planned,
        frozenset(scan.slot_map),
        context.config.channels,
    )
    keys_path: Path | None = None
    extra_outcomes: list[PairOutcome] = []
    if context.is_full:
        keys_path, extra_outcomes = _execute_full(context, selection)
    outcomes: list[PairOutcome] = [
        outcome_from_planned(item, is_dry_run=not context.is_full)
        for item in selection.planned
        if not item.is_too_late      # они в разделе «пропущено», не в исходах
    ]
    outcomes.extend(extra_outcomes)
    report: RunReport = RunReport(
        mode=context.mode,
        generated_at_text=context.generated_at_text,
        packages=build_package_lines(scan, context.config),
        outcomes=outcomes,
        orphans=[_orphan_line(orphan) for orphan in orphans],
        skipped=build_skipped_lines(scan, selection, context.config),
        mismatches=build_mismatch_lines(selection.planned),
        warnings=build_warning_lines(selection.planned, context.form_diagnostics),
        keys_file_path=display_path(context.paths.root, keys_path),
        notice=context.notice,
    )
    # новый ключ, не дошедший до стримера, — это код выхода 1; прежний ключ форму не ждёт (§7.5)
    form_pending: bool = context.is_full and any(item.is_new_key_undelivered for item in selection.planned)
    has_errors: bool = _has_error_outcomes(report.outcomes) or bool(scan.problems) or form_pending
    return _complete(context, report, has_errors=has_errors)


def _execute_full(
    context: _RunContext,
    selection: Selection,
) -> tuple[Path | None, list[PairOutcome]]:
    """Действия → форма → keys.txt (§4, §7.5)."""
    executor: _Executor = _Executor(context)
    for item in selection.planned:
        executor.execute(item)
    context.form_diagnostics.extend(_send_forms(context, selection.planned))
    keys_path, outcomes = _write_keys(
        context,
        # все будущие эфиры с ключом, включая слоты внутри min_lead_minutes (§5.5)
        [key_row_from_planned(item) for item in selection.planned if item.stream_key],
    )
    return keys_path, outcomes


def _run_status(context: _RunContext) -> RunOutcome:
    """Без promo: эфиры с маркером планера на каналах → keys.txt и отчёт."""
    marked: MarkedScan = Reconciler(context.platform).marked_broadcasts(context.config.channels)
    rows: list[KeyRow] = [key_row_from_marked(item) for item in marked.broadcasts]
    outcomes: list[PairOutcome] = [outcome_from_marked(item) for item in marked.broadcasts]
    outcomes.extend(platform_error_outcome(failure.channel, failure.error) for failure in marked.failures)
    keys_path, keys_errors = _write_keys(context, rows)
    outcomes.extend(keys_errors)
    report: RunReport = RunReport(
        mode=RunMode.STATUS,
        generated_at_text=context.generated_at_text,
        outcomes=outcomes,
        keys_file_path=display_path(context.paths.root, keys_path),
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
    return RunOutcome(report=report, exit_code=int(exit_code), report_path=report_path)


def _has_error_outcomes(outcomes: Sequence[PairOutcome]) -> bool:
    return any(outcome.kind in ERROR_OUTCOME_KINDS for outcome in outcomes)


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
        broadcast_url=broadcast_url_for(orphan.channel, orphan.broadcast.broadcast_id),
    )


def _write_keys(context: _RunContext, rows: list[KeyRow]) -> tuple[Path | None, list[PairOutcome]]:
    try:
        return write_keys_file(context.paths, render_keys_file(rows, context.generated_at_text)), []
    except OSError as error:
        LOGGER.error("keys_write_failed path=%s reason=%s", context.paths.keys_file, error)
        return None, [planer_error_outcome(context.paths.keys_file.name, ERROR_CODE_KEYS_WRITE, str(error))]


def _send_forms(context: _RunContext, planned: Sequence[PlannedBroadcast]) -> list[str]:
    """Финальный проход (§7.5): только ключ, полученный в этом запуске.

    Прежний ключ совпавшего или исправленного эфира не отправляется никогда — даже если
    прошлая отправка не подтвердилась: повтор задвоил бы ключ у стримера.
    Возвращает пути сохранённых диагностических файлов формы — они идут в отчёт.
    """
    diagnostics: list[str] = []
    for item in planned:
        if not item.is_new_key_undelivered:
            continue
        result: FormSendResult = context.form_sender.send(item)
        if result.confirmed:
            item.is_form_sent = True
            item.form_sent_at = context.now_naive
            item.last_error = None
        else:
            item.last_error = _form_error_text(result)
        if result.diagnostic_path is not None:
            diagnostics.append(str(result.diagnostic_path))
        LOGGER.info(
            "form_send slot_id=%s channel=%s confirmed=%s stream_key=%s",
            item.slot_id,
            item.channel.id,
            result.confirmed,
            mask_stream_key(item.stream_key),
        )
    return diagnostics


def _describe_spec(spec: BroadcastSpec | None) -> dict[str, object]:
    """Набор ключей для broadcast_expected и broadcast_actual — считается здесь, в одном месте."""
    if spec is None:
        return {key: MISSING_FIELD for key in SPEC_LOG_KEYS}
    return {
        "start": spec.start_minute.isoformat(),
        "marker": spec.marker or MISSING_FIELD,
        "title": _quoted(spec.title),
        "title_len": len(spec.title),
        "description_len": len(spec.description),
        "description_head": _quoted(spec.description[:DESCRIPTION_HEAD_CHARS]),
    }


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
    }


def _quoted(text: str) -> str:
    """Значение с пробелами — в кавычках: строка лога должна разбираться как key=value."""
    return '\"' + _one_line(text) + '\"'


def _one_line(text: str) -> str:
    """Переводы строк — в \\n: строка лога должна оставаться одной строкой."""
    return text.replace(chr(13), '').replace(chr(10), '\\n')


def _log_line(item: PlannedBroadcast, fields: dict[str, object]) -> str:
    identity: dict[str, object] = {"slot_id": item.slot_id, "channel": item.channel.id}
    return " ".join(f"{key}={value}" for key, value in (identity | fields).items())


def _log_broadcast_fields(item: PlannedBroadcast) -> None:
    """Что хотели, что было в списке эфиров и что лежит на платформе — для разбора расхождений."""
    LOGGER.info("broadcast_expected %s", _log_line(item, _describe_spec(item.expected)))
    LOGGER.info("broadcast_actual %s", _log_line(item, _describe_spec(item.actual)))
    if item.facts is not None:
        LOGGER.info("broadcast_facts %s", _log_line(item, _describe_facts(item.facts)))


class _Executor:
    """Действия полного запуска по решениям сверки (§7.3, §7.4); сбой объекта изолирован."""

    def __init__(self, context: _RunContext) -> None:
        self._context: _RunContext = context
        self._platform: BroadcastPlatform = context.platform

    def execute(self, item: PlannedBroadcast) -> None:
        try:
            self._dispatch(item)
            self._finish(item)
        except PlatformError as error:
            LOGGER.warning("pair_failed slot_id=%s channel=%s code=%s", item.slot_id, item.channel.id, error.code)
            item.error = OutcomeError(origin=item.channel.platform.value, code=error.code, message=error.message)
            item.last_error = error.message or error.code
            item.decision = Decision.ERROR
        except PackageError as error:
            LOGGER.error("pair_package_failed slot_id=%s reason=%s", item.slot_id, error)
            item.error = OutcomeError(origin=ERROR_ORIGIN_PACKAGE, code=error.reason.value, message=error.detail)
            item.last_error = error.detail
            item.decision = Decision.ERROR

    def _dispatch(self, item: PlannedBroadcast) -> None:
        if item.is_too_late:
            return      # до старта меньше min_lead_minutes: ключ храним, эфир не трогаем
        if item.decision is Decision.NO_STREAM:
            self._attach_stream(item)
            return
        if item.decision is Decision.CREATE:
            self._create(item)
            return
        if item.decision is Decision.UPDATE:
            self._update(item)
        # MATCH: ключ и ссылку сверка уже взяла с площадки, действий нет

    def _create(self, item: PlannedBroadcast) -> None:
        created: CreatedBroadcast = self._platform.create_broadcast(item.channel, item.expected)
        item.take_new_key(created)
        LOGGER.info(
            "broadcast_created slot_id=%s channel=%s broadcast_id=%s stream_key=%s",
            item.slot_id,
            item.channel.id,
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
            "stream_attached slot_id=%s channel=%s broadcast_id=%s stream_key=%s",
            item.slot_id,
            item.channel.id,
            attached.broadcast_id,
            mask_stream_key(attached.stream_key),
        )
        changed: tuple[ChangedField, ...] = item.actual.diff(item.expected) if item.actual else ()
        item.changed_fields = changed
        item.decision = Decision.UPDATE if changed else Decision.MATCH
        if changed:
            self._update(item)

    def _update(self, item: PlannedBroadcast) -> None:
        broadcast_id: str = item.found.broadcast_id if item.found else ""
        self._platform.update_broadcast(
            item.channel,
            broadcast_id,
            item.expected,
        )
        LOGGER.info(
            "broadcast_updated slot_id=%s channel=%s broadcast_id=%s fields=%s",
            item.slot_id,
            item.channel.id,
            broadcast_id,
            ",".join(changed.value for changed in item.changed_fields),
        )

    def _finish(self, item: PlannedBroadcast) -> None:
        """Аудитория и снимок фактов — по каждому эфиру, который планер считает своим."""
        broadcast_id: str | None = self._own_broadcast_id(item)
        if broadcast_id is None:
            return
        self._apply_video_settings(item, broadcast_id)
        self._read_facts(item, broadcast_id)

    @staticmethod
    def _own_broadcast_id(item: PlannedBroadcast) -> str | None:
        """Эфир планера: создан, привязан, исправлен или подтверждён. Прочие — не наше дело."""
        if item.error is not None or item.is_too_late:
            return None
        if item.decision not in (Decision.CREATE, Decision.UPDATE, Decision.MATCH):
            return None
        return item.broadcast_id or (item.found.broadcast_id if item.found else None)

    def _apply_video_settings(self, item: PlannedBroadcast, broadcast_id: str) -> None:
        """Язык, категория и аудитория — одним проходом по ресурсу видео (§7.4)."""
        try:
            fixes: VideoFixes = self._platform.apply_video_settings(
                item.channel,
                broadcast_id,
                item.language,
                item.channel.category_id,
            )
        except PlatformError as error:
            LOGGER.warning(
                "video_settings_failed slot_id=%s channel=%s code=%s",
                item.slot_id,
                item.channel.id,
                error.code,
            )
            item.warn(OutcomeWarning(WARNING_STEP_SETTINGS, error.code, error.message))
            return
        if fixes.audience_cleared:
            item.warn(OutcomeWarning(WARNING_STEP_AUDIENCE, "fixed"))
        if fixes.category_set and item.found is not None:
            # категория была другой только у найденного эфира: у созданного её ставит планер
            item.warn(OutcomeWarning(WARNING_STEP_CATEGORY, item.channel.category_id))

    def _read_facts(self, item: PlannedBroadcast, broadcast_id: str) -> None:
        """Один раз на объект: что по факту лежит на платформе (§5.6)."""
        try:
            item.facts = self._platform.read_facts(item.channel, broadcast_id)
        except PlatformError as error:
            LOGGER.warning(
                "facts_read_failed slot_id=%s channel=%s code=%s",
                item.slot_id,
                item.channel.id,
                error.code,
            )
            item.warn(OutcomeWarning(WARNING_STEP_FACTS, error.code, error.message))
            return
        _log_broadcast_fields(item)
        if item.facts.age_restricted:
            item.warn(OutcomeWarning(WARNING_STEP_AGE_RESTRICTED, "ytAgeRestricted", item.broadcast_url or ""))

    def _set_thumbnail(self, item: PlannedBroadcast, broadcast_id: str) -> None:
        """Превью не критично (ТЗ §7.4 п.4): эфир и ключ остаются в силе."""
        preview: bytes | None = self._preview(item)
        if preview is None:
            return
        try:
            self._platform.set_thumbnail(item.channel, broadcast_id, preview)
        except PlatformError as error:
            LOGGER.warning(
                "thumbnail_failed slot_id=%s channel=%s code=%s",
                item.slot_id,
                item.channel.id,
                error.code,
            )
            item.warn(OutcomeWarning(WARNING_STEP_THUMBNAIL, error.code, error.message))

    def _preview(self, item: PlannedBroadcast) -> bytes | None:
        """Случайное превью слота (§5.1) — если канал ставит превью и в слоте они есть."""
        if not item.channel.set_thumbnail or not item.slot.previews:
            return None
        return read_preview(item.source_package, self._context.rng.choice(item.slot.previews))
