"""Отчёт запуска (ТЗ §5.6): данные исходов, текст, файл logs\\{дата}_{время}_report.md.

Исходы хранятся как статус + данные; текст — только при рендере, из messages_ru.
Счётчики считаются один раз — build_totals; ими пользуются и отчёт, и консоль (console.py).
RunMode живёт здесь, а не в runner.py: отчёт зависит от режима, а runner собирает
отчёт, — так нет круговой зависимости (runner его реэкспортирует).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.dates import FILE_STAMP_FORMAT
from app.core.text import normalize_description, normalize_title
from app.form.base import FORM_CODE_NOT_CONFIRMED
from app.package.promo import AcceptedPackage, PromoScan, PackageProblem
from app.package.model import PackageErrorReason, Slot, slot_order_key
from app.package.reader import SCHEMA_VERSION_SUPPORTED
from app.paths import PlanerPaths
from app.pipeline.plan import Decision, OutcomeError, PlannedBroadcast
from app.pipeline.reconciler import MarkedBroadcast
from app.pipeline.selection import Selection, SkippedSlot, SkipReason
from app.platforms.base import BroadcastFacts, PlatformError, broadcast_url_for
from app.ui import messages_ru as msg

REPORT_FILE_TEMPLATE: Final[str] = "{stamp}_report.md"
REPORT_ENCODING: Final[str] = "utf-8"
MISSING_VALUE: Final[str] = "-"
# OutcomeError.origin для сбоев самого планера; расшифровка — PLANER_ERROR_TEXT в messages_ru.
PLANER_ORIGIN: Final[str] = "planer"
MISMATCH_HEAD_CHARS: Final[int] = 200   # описание в отчёт целиком не выводится


class RunMode(str, Enum):
    FULL = "full"
    DRY_RUN = "dry_run"
    STATUS = "status"


class PackageLineStatus(str, Enum):
    ACCEPTED = "accepted"
    DAMAGED = "damaged"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    ALL_PAST = "all_past"                # у пакета нет ни одного будущего слота


class OutcomeKind(str, Enum):
    CREATED = "created"
    FIXED = "fixed"
    MATCHED = "matched"
    NO_STREAM = "no_stream"   # эфир есть, привязанного потока нет — ключ взять неоткуда
    STREAM_ATTACHED = "stream_attached"   # эфир был без потока, поток привязан этим запуском
    ERROR = "error"
    AMBIGUOUS = "ambiguous"


# Требуют внимания владельца и дают код выхода 1.
ERROR_OUTCOME_KINDS: Final[frozenset[OutcomeKind]] = frozenset(
    {OutcomeKind.ERROR, OutcomeKind.AMBIGUOUS, OutcomeKind.NO_STREAM}
)
# привязанный поток — тоже новый ключ, поэтому он в разделе «Создано»
CREATED_OUTCOME_KINDS: Final[frozenset[OutcomeKind]] = frozenset({OutcomeKind.CREATED, OutcomeKind.STREAM_ATTACHED})


class SkipKind(str, Enum):
    PAST = "past"
    TOO_LATE = "too_late"
    NO_CHANNEL = "no_channel"


class FormState(str, Enum):
    """Только для нового ключа этого запуска: прежний ключ в форму не уходит (§7.5)."""

    SENT = "sent"          # ответ формы подтверждён
    FAILED = "failed"      # не подтверждён; повтора не будет


_UNREADABLE_PACKAGE_STATUSES: Final[frozenset[PackageLineStatus]] = frozenset(
    {PackageLineStatus.DAMAGED, PackageLineStatus.UNSUPPORTED_SCHEMA}
)
_PACKAGE_TEMPLATES: Final[dict[PackageLineStatus, str]] = {
    PackageLineStatus.ACCEPTED: msg.PACKAGE_ACCEPTED,
    PackageLineStatus.DAMAGED: msg.PACKAGE_DAMAGED,
    PackageLineStatus.UNSUPPORTED_SCHEMA: msg.PACKAGE_UNSUPPORTED_SCHEMA,
    PackageLineStatus.ALL_PAST: msg.PACKAGE_ALL_PAST,
}
_SKIP_TEMPLATES: Final[dict[SkipKind, str]] = {
    SkipKind.PAST: msg.SKIP_PAST,
    SkipKind.TOO_LATE: msg.SKIP_TOO_LATE,
    SkipKind.NO_CHANNEL: msg.SKIP_NO_CHANNEL,
}
_FORM_MARKS: Final[dict[FormState, str]] = {
    FormState.SENT: msg.FORM_MARK_SENT,
    FormState.FAILED: msg.FORM_MARK_FAILED,
}


@dataclass(frozen=True)
class ReportPackageLine:
    file_name: str
    status: PackageLineStatus
    slots_total: int = 0
    slots_mine: int = 0
    detail: str = ""   # причина повреждения, версия схемы или ошибка переноса


@dataclass(frozen=True)
class PairOutcome:
    kind: OutcomeKind
    account_name: str
    date: str | None = None          # None — ошибка уровня канала (без слота)
    time: str | None = None
    language: str | None = None
    broadcast_url: str | None = None
    changed_fields: tuple[str, ...] = ()   # "title", "description"
    form: FormState | None = None          # None — нового ключа нет, форма не отправлялась
    form_error: str | None = None          # причина, по которой новый ключ не ушёл (§7.5)
    error: OutcomeError | None = None


@dataclass(frozen=True)
class SkippedLine:
    """Пропущенный слот: текст собирается при рендере, консоль группирует по причине."""

    kind: SkipKind
    date: str
    time: str
    language: str
    minutes: int = 0     # только для TOO_LATE: min_lead_minutes


@dataclass(frozen=True)
class OrphanLine:
    date: str
    time: str
    language: str
    account_name: str
    broadcast_url: str


@dataclass(frozen=True)
class RunReport:
    mode: RunMode
    generated_at_text: str
    packages: list[ReportPackageLine] = field(default_factory=list)
    outcomes: list[PairOutcome] = field(default_factory=list)
    orphans: list[OrphanLine] = field(default_factory=list)
    skipped: list[SkippedLine] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    keys_file_path: str | None = None
    notice: str | None = None


@dataclass(frozen=True)
class RunTotals:
    """Счётчики запуска — единственный подсчёт для строки «Итог», заголовков разделов и консоли."""

    packages: int
    packages_unreadable: int
    slots_total: int
    slots_mine: int
    created: int          # создано + поток привязан: и там, и там новый ключ
    form_sent: int
    fixed: int
    matched: int          # в --status — эфиры планера на каналах
    orphans: int
    skipped: int
    errors: int


def build_totals(report: RunReport) -> RunTotals:
    kinds: list[OutcomeKind] = [outcome.kind for outcome in report.outcomes]
    forms: list[FormState | None] = [outcome.form for outcome in report.outcomes]
    accepted: list[ReportPackageLine] = [
        line for line in report.packages if line.status is PackageLineStatus.ACCEPTED
    ]
    return RunTotals(
        packages=len(report.packages),
        packages_unreadable=sum(1 for line in report.packages if line.status in _UNREADABLE_PACKAGE_STATUSES),
        slots_total=sum(line.slots_total for line in accepted),
        slots_mine=sum(line.slots_mine for line in accepted),
        created=sum(1 for kind in kinds if kind in CREATED_OUTCOME_KINDS),
        form_sent=forms.count(FormState.SENT),
        fixed=kinds.count(OutcomeKind.FIXED),
        matched=kinds.count(OutcomeKind.MATCHED),
        orphans=len(report.orphans),
        skipped=len(report.skipped),
        errors=sum(1 for kind in kinds if kind in ERROR_OUTCOME_KINDS),
    )


# --- строители исходов: единственный мост «объект → строка отчёта»

_DECISION_KINDS: Final[dict[Decision, OutcomeKind]] = {
    Decision.CREATE: OutcomeKind.CREATED,
    Decision.UPDATE: OutcomeKind.FIXED,
    Decision.MATCH: OutcomeKind.MATCHED,
    Decision.NO_STREAM: OutcomeKind.NO_STREAM,
    Decision.TOO_LATE: OutcomeKind.MATCHED,   # в исходы не попадает: раздел «пропущено»
    Decision.AMBIGUOUS: OutcomeKind.AMBIGUOUS,
    Decision.ERROR: OutcomeKind.ERROR,
}


def outcome_from_planned(item: PlannedBroadcast, *, is_dry_run: bool = False) -> PairOutcome:
    """Исход по объекту; в dry-run форма не отправлялась, поэтому её отметки нет."""
    kind: OutcomeKind = OutcomeKind.ERROR if item.error is not None else _DECISION_KINDS[item.decision]
    if item.stream_attached and item.error is None:
        kind = OutcomeKind.STREAM_ATTACHED
    return PairOutcome(
        kind=kind,
        account_name=item.account_name,
        date=item.date,
        time=item.time,
        language=item.language,
        broadcast_url=item.broadcast_url or item.found_url,
        changed_fields=tuple(changed.value for changed in item.changed_fields),
        form=None if is_dry_run else _form_state(item),
        form_error=item.last_error if item.is_new_key else None,
        error=item.error,
    )


def outcome_from_marked(marked: MarkedBroadcast) -> PairOutcome:
    """--status: эфир с маркером планера, найденный на канале."""
    return PairOutcome(
        kind=OutcomeKind.MATCHED,
        account_name=marked.channel.account_name,
        date=marked.parts.date,
        time=marked.parts.time,
        language=marked.parts.language,
        broadcast_url=broadcast_url_for(marked.channel, marked.broadcast.broadcast_id),
    )


def platform_error_outcome(channel: ChannelConfig, error: PlatformError) -> PairOutcome:
    return PairOutcome(
        kind=OutcomeKind.ERROR,
        account_name=channel.account_name,
        error=OutcomeError(origin=channel.platform.value, code=error.code, message=error.message),
    )


def planer_error_outcome(name: str, code: str, detail: str) -> PairOutcome:
    """Сбой самого планера: например, не записан файл ключей."""
    return PairOutcome(
        kind=OutcomeKind.ERROR,
        account_name=name,
        error=OutcomeError(origin=PLANER_ORIGIN, code=code, message=detail),
    )


def build_warning_lines(planned: Sequence[PlannedBroadcast], diagnostics: Sequence[str] = ()) -> list[str]:
    """Предупреждения (§7.4 п.4): эфир в силе, код выхода не меняется."""
    lines: list[str] = [
        msg.WARNING_LINE.format(
            prefix=_slot_text(msg.OUTCOME_SLOT_PREFIX, item.slot, account_name=item.account_name),
            step=msg.WARNING_STEP_TEXT.get(warning.step, warning.step),
            code=warning.code,
            message=warning.message,
        )
        for item in planned
        for warning in item.warnings
    ]
    if any(item.facts is not None and item.facts.live_chat_id for item in planned):
        lines.append(msg.WARNING_LIVE_CHAT)      # один раз на запуск, а не на каждый эфир
    if any(item.has_kept_key for item in planned):
        lines.append(msg.WARNING_KEPT_KEY)       # тоже один раз: правило общее для всех эфиров
    lines.extend(msg.WARNING_FORM_DIAGNOSTIC.format(path=path) for path in diagnostics)
    return lines


def _unique(lines: Iterable[SkippedLine]) -> list[SkippedLine]:
    """Один слот на несколько каналов даёт одну строку пропуска, а не несколько."""
    seen: dict[SkippedLine, None] = {}
    for line in lines:
        seen.setdefault(line, None)
    return list(seen)


def build_mismatch_lines(planned: Sequence[PlannedBroadcast]) -> list[str]:
    """Что хотели и что лежит на платформе (§5.6). Совпало всё — раздела в отчёте нет."""
    lines: list[str] = []
    for item in planned:
        if item.facts is None:
            continue
        prefix: str = _slot_text(msg.OUTCOME_SLOT_PREFIX, item.slot, account_name=item.account_name)
        lines.extend(msg.MISMATCH_LINE.format(prefix=prefix, field=field, wanted=wanted, actual=actual)
                     for field, wanted, actual in _mismatches(item, item.facts))
    return lines


def _mismatches(item: PlannedBroadcast, facts: BroadcastFacts) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    _add_if_different(found, msg.MISMATCH_FIELD_TITLE, item.expected.title, normalize_title(facts.title))
    _add_description(found, item, facts)
    if facts.start_utc is not None:
        # нет времени — сравнивать не с чем; прочерк владелец читал бы как расхождение
        _add_if_different(
            found,
            msg.MISMATCH_FIELD_START,
            item.expected.start_minute.isoformat(),
            facts.start_utc.isoformat(),
        )
    _add_if_different(found, msg.MISMATCH_FIELD_MARKER, item.expected.marker, facts.stream_marker or MISSING_VALUE)
    _add_if_different(found, msg.MISMATCH_FIELD_LANGUAGE, item.language, facts.default_language or MISSING_VALUE)
    _add_if_different(
        found,
        msg.MISMATCH_FIELD_CATEGORY,
        item.channel.category_id,
        facts.category_id or MISSING_VALUE,
    )
    if facts.made_for_kids:
        found.append((msg.MISMATCH_FIELD_AUDIENCE, msg.AUDIENCE_NOT_FOR_KIDS, msg.AUDIENCE_FOR_KIDS))
    return found


def _add_description(found: list[tuple[str, str, str]], item: PlannedBroadcast, facts: BroadcastFacts) -> None:
    """Описание целиком в отчёт не выводится: длина и начало каждой стороны."""
    wanted: str = item.expected.description
    actual: str = normalize_description(facts.description)
    if wanted == actual:
        return
    found.append(
        (
            msg.MISMATCH_FIELD_DESCRIPTION,
            msg.MISMATCH_DESCRIPTION.format(length=len(wanted), head=_head(wanted)),
            msg.MISMATCH_DESCRIPTION.format(length=len(actual), head=_head(actual)),
        )
    )


def _head(text: str) -> str:
    return text[:MISMATCH_HEAD_CHARS].replace(chr(10), ' ')


def _add_if_different(found: list[tuple[str, str, str]], field: str, wanted: str, actual: str) -> None:
    if wanted != actual:
        found.append((field, wanted, actual))


def _form_state(item: PlannedBroadcast) -> FormState | None:
    """None — нового ключа нет: прежний ключ планер в форму не отправляет (§7.5)."""
    if not item.is_new_key or not item.stream_key:
        return None
    return FormState.SENT if item.is_form_sent else FormState.FAILED


def render_report(report: RunReport) -> str:
    """Структура ТЗ §5.6: сначала итог и то, ради чего отчёт открывают, потом справка; пустой раздел не печатается."""
    totals: RunTotals = build_totals(report)
    lines: list[str] = _header_lines(report)
    if report.mode is RunMode.STATUS:
        _append_status_body(lines, report, totals)
    else:
        _append_run_body(lines, report, totals)
    return "\n".join(lines).rstrip("\n") + "\n"


def display_path(root: Path, path: Path | None) -> str | None:
    """Путь для владельца — от корня планера, как он видит папки рядом с программой."""
    if path is None:
        return None
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_report(paths: PlanerPaths, text: str, now_local: datetime) -> Path:
    report_path: Path = paths.logs_dir / REPORT_FILE_TEMPLATE.format(
        stamp=now_local.strftime(FILE_STAMP_FORMAT)
    )
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding=REPORT_ENCODING)
    return report_path


def build_package_lines(scan: PromoScan, config: PlanerConfig) -> list[ReportPackageLine]:
    all_past: set[Path] = {package.path for package in scan.all_past_packages}
    lines: list[ReportPackageLine] = [
        _accepted_line(item, config, is_all_past=item.package.path in all_past)
        for item in scan.packages
    ]
    lines.extend(_problem_line(problem) for problem in scan.problems)
    return lines


def build_skipped_lines(scan: PromoScan, selection: Selection, config: PlanerConfig) -> list[SkippedLine]:
    """«Уже прошло» — только слоты моих языков; плюс too_late / no_channel из отбора (§7.2)."""
    entries: list[tuple[Slot, SkippedLine]] = [
        (slot, _skipped_line(SkipKind.PAST, slot))
        for slot in scan.past_slots
        if slot.language in config.served_languages
    ]
    entries.extend(
        (item.slot, _skipped_line(SkipKind.TOO_LATE, item.slot, minutes=config.min_lead_minutes))
        for item in selection.planned
        if item.is_too_late
    )
    entries.extend((skipped.slot, _skipped_from_selection(skipped)) for skipped in selection.skipped)
    entries.sort(key=lambda entry: slot_order_key(entry[0]))
    return _unique(line for _, line in entries)


def _header_lines(report: RunReport) -> list[str]:
    title: str = msg.REPORT_TITLE.format(generated_at=report.generated_at_text)
    if report.mode is RunMode.DRY_RUN:
        title += msg.REPORT_TITLE_DRY_RUN
    lines: list[str] = [title]
    if report.notice:
        lines.append(msg.REPORT_NOTICE.format(notice=report.notice))
    lines.append("")
    return lines


def _append_run_body(lines: list[str], report: RunReport, totals: RunTotals) -> None:
    is_dry_run: bool = report.mode is RunMode.DRY_RUN
    texts: dict[OutcomeKind, list[str]] = {
        kind: [_outcome_text(outcome, is_dry_run=is_dry_run) for outcome in report.outcomes if outcome.kind is kind]
        for kind in OutcomeKind
    }
    lines.extend(_total_lines(report, totals))
    _append_section(lines, msg.REPORT_SECTION_ERRORS, [
        _outcome_text(outcome, is_dry_run=is_dry_run) for outcome in report.outcomes if outcome.kind in ERROR_OUTCOME_KINDS
    ])
    _append_section(lines, msg.REPORT_SECTION_WARNINGS, report.warnings)
    _append_section(lines, msg.REPORT_SECTION_MISMATCHES, report.mismatches)
    created: list[str] = texts[OutcomeKind.CREATED] + texts[OutcomeKind.STREAM_ATTACHED]
    _append_section(lines, msg.REPORT_SECTION_CREATED.format(count=totals.created), created)
    _append_section(lines, msg.REPORT_SECTION_FIXED.format(count=totals.fixed), texts[OutcomeKind.FIXED])
    _append_section(lines, msg.REPORT_SECTION_MATCHED.format(count=totals.matched), texts[OutcomeKind.MATCHED])
    _append_section(
        lines,
        msg.REPORT_SECTION_ORPHANS.format(count=totals.orphans),
        [_orphan_text(orphan) for orphan in report.orphans],
    )
    _append_section(lines, msg.REPORT_SECTION_SKIPPED, [skip_text(line) for line in report.skipped])
    _append_section(lines, msg.REPORT_SECTION_PACKAGES, [render_package_line(line) for line in report.packages])


def _total_lines(report: RunReport, totals: RunTotals) -> list[str]:
    if report.mode is RunMode.STATUS:
        total: str = msg.REPORT_STATUS_TOTAL.format(
            scheduled=totals.matched, errors=totals.errors, keys_file=_keys_file_part(report)
        )
    else:
        total = msg.REPORT_TOTAL.format(
            created=totals.created,
            fixed=totals.fixed,
            matched=totals.matched,
            skipped=totals.skipped,
            errors=totals.errors,
            keys_file=_keys_file_part(report),
        )
    return [total, ""]


def _append_status_body(lines: list[str], report: RunReport, totals: RunTotals) -> None:
    lines.extend(_total_lines(report, totals))
    _append_section(lines, msg.REPORT_SECTION_ERRORS, error_texts(report))
    _append_section(
        lines,
        msg.REPORT_SECTION_SCHEDULED.format(count=totals.matched),
        [
            msg.SCHEDULED_LINE.format(prefix=outcome_prefix(outcome), url=outcome.broadcast_url or MISSING_VALUE)
            for outcome in report.outcomes
            if outcome.kind is OutcomeKind.MATCHED
        ],
    )


def _append_section(lines: list[str], header: str, body: list[str]) -> None:
    """Раздел без строк не печатается вовсе: владелец не читает заголовки с нулями."""
    if not body:
        return
    lines.append(header)
    lines.extend(msg.REPORT_ITEM.format(text=text) for text in body)
    lines.append("")


def error_texts(report: RunReport) -> list[str]:
    """Ошибки полным текстом — одинаково для отчёта --status и консоли."""
    return [_outcome_body(outcome, is_dry_run=False) for outcome in report.outcomes if outcome.kind in ERROR_OUTCOME_KINDS]


def package_problem_texts(report: RunReport) -> list[str]:
    """Непрочитанные пакеты: в отчёте — в разделе «Пакеты», в консоли — строками внимания."""
    return [render_package_line(line) for line in report.packages if line.status in _UNREADABLE_PACKAGE_STATUSES]


def _keys_file_part(report: RunReport) -> str:
    return msg.REPORT_TOTAL_KEYS_FILE.format(path=report.keys_file_path) if report.keys_file_path else ""


def outcome_prefix(outcome: PairOutcome) -> str:
    if outcome.date is None:
        return msg.OUTCOME_CHANNEL_PREFIX.format(account_name=outcome.account_name)
    return msg.OUTCOME_SLOT_PREFIX.format(
        date=outcome.date,
        time=outcome.time,
        language=outcome.language,
        account_name=outcome.account_name,
    )


def _outcome_text(outcome: PairOutcome, *, is_dry_run: bool) -> str:
    body: str = _outcome_body(outcome, is_dry_run=is_dry_run)
    return body + msg.OUTCOME_DRY_RUN_SUFFIX if is_dry_run else body


def _outcome_body(outcome: PairOutcome, *, is_dry_run: bool) -> str:
    prefix: str = outcome_prefix(outcome)
    if outcome.kind is OutcomeKind.CREATED:
        return _created_text(outcome, prefix, is_dry_run=is_dry_run)
    if outcome.kind is OutcomeKind.FIXED:
        return _fixed_text(outcome, prefix, is_dry_run=is_dry_run)
    if outcome.kind is OutcomeKind.MATCHED:
        return _matched_text(outcome, prefix)
    if outcome.kind is OutcomeKind.AMBIGUOUS:
        return msg.OUTCOME_AMBIGUOUS.format(prefix=prefix)
    if outcome.kind is OutcomeKind.NO_STREAM:
        return msg.OUTCOME_NO_STREAM.format(prefix=prefix, url=outcome.broadcast_url or MISSING_VALUE)
    if outcome.kind is OutcomeKind.STREAM_ATTACHED:
        return msg.OUTCOME_STREAM_ATTACHED.format(prefix=prefix, form=form_mark(outcome.form, outcome.form_error))
    return _error_text(outcome, prefix)


def form_mark(form: FormState | None, error: str | None = None) -> str:
    if form is None:
        return MISSING_VALUE
    if form is FormState.FAILED:
        return msg.FORM_MARK_FAILED.format(reason=form_reason_text(error))
    return _FORM_MARKS[form]


def form_reason_text(error: str | None) -> str:
    """Код исхода отправки «code: detail» → текст для владельца (§7.5); единый для отчёта и keys.txt."""
    if not error:
        return msg.FORM_REASON_TEXT[FORM_CODE_NOT_CONFIRMED].format(detail=FORM_CODE_NOT_CONFIRMED)
    code, _, detail = error.partition(": ")
    template: str = msg.FORM_REASON_TEXT.get(code, msg.FORM_REASON_UNKNOWN)
    return template.format(detail=detail or code)


def _created_text(outcome: PairOutcome, prefix: str, *, is_dry_run: bool) -> str:
    if is_dry_run:
        return msg.OUTCOME_CREATE_PLANNED.format(prefix=prefix)
    return msg.OUTCOME_CREATED.format(prefix=prefix, form=form_mark(outcome.form, outcome.form_error))


def changed_fields_text(outcome: PairOutcome) -> str:
    return msg.CHANGED_FIELDS_JOINER.join(msg.CHANGED_FIELD_TEXT[name] for name in outcome.changed_fields)


def _fixed_text(outcome: PairOutcome, prefix: str, *, is_dry_run: bool) -> str:
    template: str = msg.OUTCOME_FIX_PLANNED if is_dry_run else msg.OUTCOME_FIXED
    return template.format(prefix=prefix, what=changed_fields_text(outcome))


def _matched_text(outcome: PairOutcome, prefix: str) -> str:
    return msg.OUTCOME_MATCHED.format(prefix=prefix, url=outcome.broadcast_url or MISSING_VALUE)


def _error_text(outcome: PairOutcome, prefix: str) -> str:
    error: OutcomeError = outcome.error or OutcomeError(origin=MISSING_VALUE, code=MISSING_VALUE)
    if error.origin in msg.PLANER_ERROR_TEXT_ORIGINS:
        text: str = msg.PLANER_ERROR_TEXT.get(error.code, error.code).format(detail=error.message)
        return msg.OUTCOME_PLANER_ERROR.format(prefix=prefix, text=text)
    return msg.OUTCOME_ERROR.format(
        prefix=prefix,
        origin=msg.ERROR_ORIGIN_TEXT.get(error.origin, error.origin),
        code=error.code,
        message=error.message,
    )


def _orphan_text(orphan: OrphanLine) -> str:
    return msg.ORPHAN_LINE.format(
        date=orphan.date,
        time=orphan.time,
        language=orphan.language,
        account_name=orphan.account_name,
        url=orphan.broadcast_url,
    )


def render_package_line(line: ReportPackageLine) -> str:
    return _PACKAGE_TEMPLATES[line.status].format(
        file=line.file_name,
        total=line.slots_total,
        mine=line.slots_mine,
        reason=line.detail,
        version=line.detail,
        supported=SCHEMA_VERSION_SUPPORTED,
        error=line.detail,
    )


def _accepted_line(item: AcceptedPackage, config: PlanerConfig, *, is_all_past: bool) -> ReportPackageLine:
    path: Path = item.package.path
    if is_all_past:
        return ReportPackageLine(path.name, PackageLineStatus.ALL_PAST)
    slots_mine: int = sum(1 for slot in item.package.slots if slot.language in config.served_languages)
    return ReportPackageLine(
        path.name,
        PackageLineStatus.ACCEPTED,
        slots_total=item.slots_total,
        slots_mine=slots_mine,
    )


def _problem_line(problem: PackageProblem) -> ReportPackageLine:
    if problem.reason is PackageErrorReason.UNSUPPORTED_SCHEMA:
        return ReportPackageLine(problem.file.name, PackageLineStatus.UNSUPPORTED_SCHEMA, detail=problem.detail)
    reason_text: str = msg.PACKAGE_REASON_WITH_DETAIL.format(
        reason=msg.PACKAGE_REASON_TEXT[problem.reason.value],
        detail=problem.detail,
    )
    return ReportPackageLine(problem.file.name, PackageLineStatus.DAMAGED, detail=reason_text)


def _skipped_line(kind: SkipKind, slot: Slot, *, minutes: int = 0) -> SkippedLine:
    return SkippedLine(kind=kind, date=slot.date, time=slot.time, language=slot.language, minutes=minutes)


def _skipped_from_selection(skipped: SkippedSlot) -> SkippedLine:
    if skipped.reason is SkipReason.NO_CHANNEL:
        return _skipped_line(SkipKind.NO_CHANNEL, skipped.slot)
    raise ValueError(f'unknown skip reason {skipped.reason}')


def skip_text(line: SkippedLine) -> str:
    return _SKIP_TEMPLATES[line.kind].format(
        date=line.date, time=line.time, language=line.language, minutes=line.minutes
    )


def _slot_text(template: str, slot: Slot, **extra: object) -> str:
    return template.format(date=slot.date, time=slot.time, language=slot.language, **extra)
