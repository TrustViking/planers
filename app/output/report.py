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
from app.form.base import FORM_CODE_NOT_CONFIRMED
from app.package.bcast import AcceptedPackage, BcastScan, PackageProblem
from app.package.model import PackageErrorReason, Slot, slot_order_key
from app.package.reader import SCHEMA_VERSION_SUPPORTED
from app.paths import PlanerPaths
from app.pipeline.plan import (
    WARNING_STEP_AMBIGUOUS,
    WARNING_STEP_REPORTED_FIELD,
    BroadcastSpec,
    ChangedField,
    Decision,
    OutcomeError,
    OutcomeWarning,
    PlannedBroadcast,
    SpecValue,
)
from app.pipeline.reconciler import MarkedBroadcast
from app.pipeline.selection import Selection, SkippedSlot, SkipReason
from app.platforms.base import BroadcastFacts, PlatformError, PlatformLimits, broadcast_url_for
from app.ui import messages_ru as msg
from app.version import APP_VERSION

REPORT_FILE_TEMPLATE: Final[str] = "{stamp}_report.md"
REPORT_ENCODING: Final[str] = "utf-8"
MISSING_VALUE: Final[str] = "-"
# OutcomeError.origin для сбоев самого планера; расшифровка — PLANER_ERROR_TEXT в messages_ru.
PLANER_ORIGIN: Final[str] = "planer"
MISMATCH_HEAD_CHARS: Final[int] = 200   # описание в отчёт целиком не выводится
# Постоянные особенности площадки: не про этот запуск, поэтому только в отчёте, в конце (ТЗ §5.6).
PLATFORM_NOTE_LINES: Final[tuple[str, ...]] = (msg.WARNING_LIVE_CHAT, msg.WARNING_KEPT_KEY)


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
    """Только для ключа, который в этом запуске должен был дойти до стримера (§7.5)."""

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
    changed_fields: tuple[str, ...] = ()   # значения ChangedField: "title", "privacy", ...
    form: FormState | None = None          # None — ключ в этом запуске в форму не шёл
    form_error: str | None = None          # причина, по которой ключ не ушёл (§7.5)
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

    @property
    def run_warnings(self) -> list[str]:
        """Предупреждения этого запуска — в консоль и в отчёт."""
        return [line for line in self.warnings if line not in PLATFORM_NOTE_LINES]

    @property
    def notes(self) -> list[str]:
        """Постоянные особенности площадки из warnings — только в отчёт, разделом в конце."""
        return [line for line in self.warnings if line in PLATFORM_NOTE_LINES]


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
        # сводка «ключ передан в форму: N из M» стоит у «создано»: считаем только созданные
        form_sent=sum(
            1
            for kind, form in zip(kinds, forms)
            if kind in CREATED_OUTCOME_KINDS and form is FormState.SENT
        ),
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
        form_error=item.last_error if item.should_send_key else None,
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
    """Два списка одним результатом: предупреждения запуска, затем постоянные особенности площадки.

    Разделяют их RunReport.run_warnings и RunReport.notes по PLATFORM_NOTE_LINES.
    """
    return build_run_warning_lines(planned, diagnostics) + build_platform_note_lines(planned)


def build_run_warning_lines(planned: Sequence[PlannedBroadcast], diagnostics: Sequence[str] = ()) -> list[str]:
    """Предупреждения этого запуска (§7.4 п.4): эфир в силе, код выхода не меняется."""
    lines: list[str] = [_warning_text(item, warning) for item in planned for warning in item.warnings]
    lines.extend(msg.WARNING_FORM_DIAGNOSTIC.format(path=path) for path in diagnostics)
    return lines


def _warning_text(item: PlannedBroadcast, warning: OutcomeWarning) -> str:
    """Предупреждения сверки собирают текст из объекта: значения полей и ссылки на эфиры."""
    prefix: str = _slot_text(msg.OUTCOME_SLOT_PREFIX, item.slot, account_name=item.account_name)
    if warning.step == WARNING_STEP_REPORTED_FIELD and item.actual is not None:
        name: ChangedField = ChangedField(warning.code)
        return msg.WARNING_REPORTED_FIELD.format(
            prefix=prefix,
            field=msg.CHANGED_FIELD_TEXT[name.value],
            wanted=spec_value_text(item.expected.value(name)),
            actual=spec_value_text(item.actual.value(name)),
        )
    if warning.step == WARNING_STEP_AMBIGUOUS:
        return msg.WARNING_AMBIGUOUS.format(prefix=prefix, urls=msg.AMBIGUOUS_URL_JOINER.join(item.ambiguous_urls))
    return msg.WARNING_LINE.format(
        prefix=prefix,
        step=msg.WARNING_STEP_TEXT.get(warning.step, warning.step),
        code=warning.code,
        message=warning.message,
    )


def spec_value_text(value: SpecValue) -> str:
    """Значение поля спеки для владельца: да/нет, прочерк, как есть."""
    if value is None or value == "":
        return MISSING_VALUE
    if isinstance(value, bool):
        return msg.SPEC_VALUE_TRUE if value else msg.SPEC_VALUE_FALSE
    return value


def build_platform_note_lines(planned: Sequence[PlannedBroadcast]) -> list[str]:
    """Так устроена площадка, это не про сегодняшний запуск: по строке на особенность, а не на эфир."""
    lines: list[str] = []
    if any(item.facts is not None and item.facts.live_chat_id for item in planned):
        lines.append(msg.WARNING_LIVE_CHAT)
    if any(item.has_kept_key for item in planned):
        lines.append(msg.WARNING_KEPT_KEY)
    return lines


def _unique(lines: Iterable[SkippedLine]) -> list[SkippedLine]:
    """Один слот на несколько каналов даёт одну строку пропуска, а не несколько."""
    seen: dict[SkippedLine, None] = {}
    for line in lines:
        seen.setdefault(line, None)
    return list(seen)


def build_mismatch_lines(planned: Sequence[PlannedBroadcast], limits: PlatformLimits) -> list[str]:
    """Что хотели и что лежит на платформе (§5.6). Совпало всё — раздела в отчёте нет."""
    lines: list[str] = []
    for item in planned:
        if item.facts is None:
            continue
        prefix: str = _slot_text(msg.OUTCOME_SLOT_PREFIX, item.slot, account_name=item.account_name)
        lines.extend(msg.MISMATCH_LINE.format(prefix=prefix, field=field, wanted=wanted, actual=actual)
                     for field, wanted, actual in _mismatches(item, limits))
    return lines


def _mismatches(item: PlannedBroadcast, limits: PlatformLimits) -> list[tuple[str, str, str]]:
    """Поля спеки — тем же diff, что решает сверку: новое поле нельзя забыть добавить сюда."""
    if item.facts is None:
        return []
    facts_spec: BroadcastSpec = BroadcastSpec.from_facts(item.facts, limits, item.expected.start_minute)
    found: list[tuple[str, str, str]] = [
        _spec_mismatch(name, item.expected, facts_spec)
        for name in facts_spec.diff(item.expected)
        if not (name is ChangedField.MARKER and _is_stream_new(item))
    ]
    facts: BroadcastFacts = item.facts
    if facts.start_utc is not None:
        # нет времени — сравнивать не с чем; прочерк владелец читал бы как расхождение
        _add_if_different(
            found,
            msg.MISMATCH_FIELD_START,
            item.expected.start_minute.isoformat(),
            facts.start_utc.isoformat(),
        )
    _add_if_different(found, msg.MISMATCH_FIELD_LANGUAGE, item.language, facts.default_language or MISSING_VALUE)
    if facts.made_for_kids:
        found.append((msg.MISMATCH_FIELD_AUDIENCE, msg.AUDIENCE_NOT_FOR_KIDS, msg.AUDIENCE_FOR_KIDS))
    return found


def _is_stream_new(item: PlannedBroadcast) -> bool:
    """Поток создан или привязан этим запуском: перечитывание фактов может его ещё не видеть (14-09-2026)."""
    return item.decision is Decision.CREATE or item.stream_attached


def _spec_mismatch(name: ChangedField, wanted: BroadcastSpec, actual: BroadcastSpec) -> tuple[str, str, str]:
    """Описание целиком в отчёт не выводится: длина и начало каждой стороны."""
    label: str = msg.CHANGED_FIELD_TEXT[name.value]
    if name is ChangedField.DESCRIPTION:
        return (
            label,
            msg.MISMATCH_DESCRIPTION.format(length=len(wanted.description), head=_head(wanted.description)),
            msg.MISMATCH_DESCRIPTION.format(length=len(actual.description), head=_head(actual.description)),
        )
    return label, spec_value_text(wanted.value(name)), spec_value_text(actual.value(name))


def _head(text: str) -> str:
    return text[:MISMATCH_HEAD_CHARS].replace(chr(10), ' ')


def _add_if_different(found: list[tuple[str, str, str]], field: str, wanted: str, actual: str) -> None:
    if wanted != actual:
        found.append((field, wanted, actual))


def _form_state(item: PlannedBroadcast) -> FormState | None:
    """None — в этом запуске ключ в форму не шёл: эфир с меткой планера совпал (§7.5)."""
    if not item.should_send_key or not item.stream_key:
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


def build_package_lines(scan: BcastScan, config: PlanerConfig) -> list[ReportPackageLine]:
    all_past: set[Path] = {package.path for package in scan.all_past_packages}
    lines: list[ReportPackageLine] = [
        _accepted_line(item, config, is_all_past=item.package.path in all_past)
        for item in scan.packages
    ]
    lines.extend(_problem_line(problem) for problem in scan.problems)
    return lines


def build_skipped_lines(scan: BcastScan, selection: Selection, config: PlanerConfig) -> list[SkippedLine]:
    """«Уже прошло» — только слоты моих языков; плюс too_late / no_channel из отбора (§7.2)."""
    entries: list[tuple[Slot, SkippedLine]] = [
        (slot, _skipped_line(SkipKind.PAST, slot))
        for slot in scan.past_slots
        if slot.language in config.served_languages
    ]
    entries.extend(
        (item.slot, _skipped_line(SkipKind.TOO_LATE, item.slot, minutes=config.settings.min_lead_minutes))
        for item in selection.planned
        if item.is_too_late
    )
    entries.extend((skipped.slot, _skipped_from_selection(skipped)) for skipped in selection.skipped)
    entries.sort(key=lambda entry: slot_order_key(entry[0]))
    return _unique(line for _, line in entries)


def _header_lines(report: RunReport) -> list[str]:
    title: str = msg.REPORT_TITLE.format(version=APP_VERSION, generated_at=report.generated_at_text)
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
    _append_section(lines, msg.REPORT_SECTION_NOT_DELIVERED, not_delivered_texts(report))
    _append_section(lines, msg.REPORT_SECTION_ERRORS, [
        _outcome_text(outcome, is_dry_run=is_dry_run) for outcome in report.outcomes if outcome.kind in ERROR_OUTCOME_KINDS
    ])
    _append_section(lines, msg.REPORT_SECTION_WARNINGS, report.run_warnings)
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
    _append_section(lines, msg.REPORT_SECTION_NOTES, report.notes)


def not_delivered_texts(report: RunReport) -> list[str]:
    """Новый ключ, который форма не подтвердила: эфир стоит, а стример ключа не получил (§7.5).

    Отдельный раздел только для глаз: исход остаётся в «Создано», счётчики build_totals не меняются.
    """
    return [
        msg.NOT_DELIVERED_LINE.format(prefix=outcome_prefix(outcome), reason=form_reason_text(outcome.form_error))
        for outcome in report.outcomes
        if outcome.form is FormState.FAILED
    ]


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
    if is_dry_run:
        return msg.OUTCOME_FIX_PLANNED.format(prefix=prefix, what=changed_fields_text(outcome))
    form: str = msg.OUTCOME_FIXED_FORM.format(mark=form_mark(outcome.form, outcome.form_error)) if outcome.form else ""
    return msg.OUTCOME_FIXED.format(prefix=prefix, what=changed_fields_text(outcome), form=form)


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
