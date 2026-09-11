"""Отчёт запуска (ТЗ §5.6): данные исходов, текст, файл reports\\report_*.md.

Исходы хранятся как статус + данные; текст — только при рендере, из messages_ru.
RunMode живёт здесь, а не в runner.py: отчёт зависит от режима, а runner собирает
отчёт, — так нет круговой зависимости (runner его реэкспортирует).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Final

from app.config.loader import PlanerConfig
from app.core.dates import REPORT_STAMP_FORMAT
from app.package.inbox import AcceptedPackage, InboxScan, PackageProblem
from app.package.model import PackageErrorReason, Slot, slot_order_key
from app.package.reader import SCHEMA_VERSION_SUPPORTED
from app.paths import PlanerPaths
from app.pipeline.selection import Selection, SkippedSlot, SkipReason
from app.ui import messages_ru as msg

REPORT_FILE_TEMPLATE: Final[str] = "report_{stamp}.md"
REPORT_ENCODING: Final[str] = "utf-8"
MISSING_VALUE: Final[str] = "-"


class RunMode(str, Enum):
    FULL = "full"
    DRY_RUN = "dry_run"
    STATUS = "status"


class PackageLineStatus(str, Enum):
    ACCEPTED = "accepted"
    DAMAGED = "damaged"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    ALL_PAST_ARCHIVED = "all_past_archived"
    ALL_PAST_KEPT = "all_past_kept"      # dry-run: не перенесён
    ARCHIVE_FAILED = "archive_failed"
    FINISHED = "finished"                # все слоты терминальны — перенесён в inbox\done
    FINISH_FAILED = "finish_failed"


class OutcomeKind(str, Enum):
    CREATED = "created"
    FIXED = "fixed"
    MATCHED = "matched"
    ERROR = "error"
    AMBIGUOUS = "ambiguous"


ERROR_OUTCOME_KINDS: Final[frozenset[OutcomeKind]] = frozenset({OutcomeKind.ERROR, OutcomeKind.AMBIGUOUS})


class FormState(str, Enum):
    SENT = "sent"          # ответ формы подтверждён
    FAILED = "failed"      # не подтверждён — повтор в следующий запуск
    WAITING = "waiting"    # отправка не выполнялась (NoopFormSender до этапа 4)


_PACKAGE_TEMPLATES: Final[dict[PackageLineStatus, str]] = {
    PackageLineStatus.ACCEPTED: msg.PACKAGE_ACCEPTED,
    PackageLineStatus.DAMAGED: msg.PACKAGE_DAMAGED,
    PackageLineStatus.UNSUPPORTED_SCHEMA: msg.PACKAGE_UNSUPPORTED_SCHEMA,
    PackageLineStatus.ALL_PAST_ARCHIVED: msg.PACKAGE_ALL_PAST_ARCHIVED,
    PackageLineStatus.ALL_PAST_KEPT: msg.PACKAGE_ALL_PAST_KEPT,
    PackageLineStatus.ARCHIVE_FAILED: msg.PACKAGE_ARCHIVE_FAILED,
    PackageLineStatus.FINISHED: msg.PACKAGE_FINISHED,
    PackageLineStatus.FINISH_FAILED: msg.PACKAGE_FINISH_FAILED,
}
_FORM_MARKS: Final[dict[FormState, str]] = {
    FormState.SENT: msg.FORM_MARK_SENT,
    FormState.FAILED: msg.FORM_MARK_FAILED,
    FormState.WAITING: msg.FORM_MARK_WAITING,
}


@dataclass(frozen=True)
class ReportPackageLine:
    file_name: str
    status: PackageLineStatus
    slots_total: int = 0
    slots_mine: int = 0
    detail: str = ""   # причина повреждения, версия схемы или ошибка переноса


@dataclass(frozen=True)
class OutcomeError:
    origin: str        # "youtube" (Platform) | "registry" | "package" | "planer"
    code: str
    message: str = ""


@dataclass(frozen=True)
class PairOutcome:
    kind: OutcomeKind
    account_name: str
    date: str | None = None          # None — ошибка уровня канала (без слота)
    time: str | None = None
    language: str | None = None
    broadcast_url: str | None = None
    changed_fields: tuple[str, ...] = ()   # "title", "description"
    form: FormState | None = None          # None — форма в этом запуске не отправлялась
    recreated: bool = False
    rebind: bool = False
    error: OutcomeError | None = None


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
    owner: str
    packages: list[ReportPackageLine] = field(default_factory=list)
    outcomes: list[PairOutcome] = field(default_factory=list)
    orphans: list[OrphanLine] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    keys_file_path: str | None = None
    notice: str | None = None


@dataclass(frozen=True)
class MoveOutcome:
    """Переносы пакетов (§7.1 п.4): в archive\\, в done\\, неудачи с причиной."""

    archived: frozenset[Path] = frozenset()
    finished: frozenset[Path] = frozenset()
    failed: dict[Path, str] = field(default_factory=dict)


def render_report(report: RunReport) -> str:
    """Структура ТЗ §5.6; пустой раздел — заголовок без строк."""
    lines: list[str] = _header_lines(report)
    if report.mode is RunMode.STATUS:
        _append_status_body(lines, report)
    else:
        _append_run_body(lines, report)
    return "\n".join(lines) + "\n"


def write_report(paths: PlanerPaths, text: str, now_local: datetime) -> Path:
    report_path: Path = paths.reports_dir / REPORT_FILE_TEMPLATE.format(
        stamp=now_local.strftime(REPORT_STAMP_FORMAT)
    )
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding=REPORT_ENCODING)
    return report_path


def build_package_lines(scan: InboxScan, config: PlanerConfig, move: MoveOutcome) -> list[ReportPackageLine]:
    all_past: set[Path] = {package.path for package in scan.all_past_packages}
    lines: list[ReportPackageLine] = [
        _accepted_line(item, config, is_all_past=item.package.path in all_past, move=move)
        for item in scan.packages
    ]
    lines.extend(_problem_line(problem) for problem in scan.problems)
    return lines


def build_skipped_lines(scan: InboxScan, selection: Selection, config: PlanerConfig) -> list[str]:
    """«Уже прошло» — только слоты моих языков; плюс too_late / no_channel из отбора (§7.2)."""
    entries: list[tuple[Slot, str]] = [
        (slot, _slot_text(msg.SKIP_PAST, slot))
        for slot in scan.past_slots
        if slot.language in config.served_languages
    ]
    entries.extend((skipped.slot, _skip_text(skipped, config)) for skipped in selection.skipped)
    entries.sort(key=lambda entry: slot_order_key(entry[0]))
    return [text for _, text in entries]


def _header_lines(report: RunReport) -> list[str]:
    title: str = msg.REPORT_TITLE.format(generated_at=report.generated_at_text, owner=report.owner)
    if report.mode is RunMode.DRY_RUN:
        title += msg.REPORT_TITLE_DRY_RUN
    lines: list[str] = [title]
    if report.notice:
        lines.append(msg.REPORT_NOTICE.format(notice=report.notice))
    lines.append("")
    return lines


def _append_run_body(lines: list[str], report: RunReport) -> None:
    is_dry_run: bool = report.mode is RunMode.DRY_RUN
    texts: dict[OutcomeKind, list[str]] = {
        kind: [_outcome_text(outcome, is_dry_run=is_dry_run) for outcome in report.outcomes if outcome.kind is kind]
        for kind in OutcomeKind
    }
    errors: list[str] = [
        _outcome_text(outcome, is_dry_run=is_dry_run)
        for outcome in report.outcomes
        if outcome.kind in ERROR_OUTCOME_KINDS
    ]
    _append_section(lines, msg.REPORT_SECTION_PACKAGES, [_render_package_line(line) for line in report.packages])
    for kind, header in (
        (OutcomeKind.CREATED, msg.REPORT_SECTION_CREATED),
        (OutcomeKind.FIXED, msg.REPORT_SECTION_FIXED),
        (OutcomeKind.MATCHED, msg.REPORT_SECTION_MATCHED),
    ):
        _append_section(lines, header.format(count=len(texts[kind])), texts[kind])
    if report.orphans:
        _append_section(
            lines,
            msg.REPORT_SECTION_ORPHANS.format(count=len(report.orphans)),
            [_orphan_text(orphan) for orphan in report.orphans],
        )
    _append_section(lines, msg.REPORT_SECTION_SKIPPED, report.skipped)
    _append_section(lines, msg.REPORT_SECTION_ERRORS, errors)
    lines.append(
        msg.REPORT_TOTAL.format(
            created=len(texts[OutcomeKind.CREATED]),
            fixed=len(texts[OutcomeKind.FIXED]),
            matched=len(texts[OutcomeKind.MATCHED]),
            skipped=len(report.skipped),
            errors=len(errors),
            keys_file=_keys_file_part(report),
        )
    )


def _append_status_body(lines: list[str], report: RunReport) -> None:
    scheduled: list[str] = [
        msg.SCHEDULED_LINE.format(prefix=_outcome_prefix(outcome), url=outcome.broadcast_url or MISSING_VALUE)
        for outcome in report.outcomes
        if outcome.kind is OutcomeKind.MATCHED
    ]
    errors: list[str] = [
        _outcome_body(outcome, is_dry_run=False)
        for outcome in report.outcomes
        if outcome.kind in ERROR_OUTCOME_KINDS
    ]
    _append_section(lines, msg.REPORT_SECTION_SCHEDULED.format(count=len(scheduled)), scheduled)
    _append_section(lines, msg.REPORT_SECTION_ERRORS, errors)
    lines.append(
        msg.REPORT_STATUS_TOTAL.format(scheduled=len(scheduled), errors=len(errors), keys_file=_keys_file_part(report))
    )


def _append_section(lines: list[str], header: str, body: list[str]) -> None:
    lines.append(header)
    lines.extend(body)
    lines.append("")


def _keys_file_part(report: RunReport) -> str:
    return msg.REPORT_TOTAL_KEYS_FILE.format(path=report.keys_file_path) if report.keys_file_path else ""


def _outcome_prefix(outcome: PairOutcome) -> str:
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
    prefix: str = _outcome_prefix(outcome)
    if outcome.kind is OutcomeKind.CREATED:
        return _created_text(outcome, prefix, is_dry_run=is_dry_run)
    if outcome.kind is OutcomeKind.FIXED:
        return _fixed_text(outcome, prefix, is_dry_run=is_dry_run)
    if outcome.kind is OutcomeKind.MATCHED:
        return _matched_text(outcome, prefix, is_dry_run=is_dry_run)
    if outcome.kind is OutcomeKind.AMBIGUOUS:
        return msg.OUTCOME_AMBIGUOUS.format(prefix=prefix)
    return _error_text(outcome, prefix)


def _form_mark(form: FormState | None) -> str:
    return _FORM_MARKS[form] if form is not None else MISSING_VALUE


def _created_text(outcome: PairOutcome, prefix: str, *, is_dry_run: bool) -> str:
    if is_dry_run:
        template: str = msg.OUTCOME_RECREATE_PLANNED if outcome.recreated else msg.OUTCOME_CREATE_PLANNED
        return template.format(prefix=prefix)
    template = msg.OUTCOME_RECREATED if outcome.recreated else msg.OUTCOME_CREATED
    return template.format(prefix=prefix, form=_form_mark(outcome.form))


def _fixed_text(outcome: PairOutcome, prefix: str, *, is_dry_run: bool) -> str:
    what: str = msg.CHANGED_FIELDS_JOINER.join(msg.CHANGED_FIELD_TEXT[name] for name in outcome.changed_fields)
    if is_dry_run:
        return msg.OUTCOME_FIX_PLANNED.format(prefix=prefix, what=what)
    if outcome.rebind:
        form_part: str = msg.FIXED_FORM_REBIND.format(form=_form_mark(outcome.form))
    elif outcome.form is not None:
        form_part = msg.FIXED_FORM_RESENT.format(form=_form_mark(outcome.form))
    else:
        form_part = msg.FIXED_FORM_NOT_RESENT
    return msg.OUTCOME_FIXED.format(prefix=prefix, what=what, form_part=form_part)


def _matched_text(outcome: PairOutcome, prefix: str, *, is_dry_run: bool) -> str:
    if is_dry_run:
        suffix: str = msg.MATCHED_SUFFIX_REBIND_PLANNED if outcome.rebind else ""
    elif outcome.rebind:
        suffix = msg.MATCHED_SUFFIX_REBIND.format(form=_form_mark(outcome.form))
    elif outcome.form is not None:
        suffix = msg.MATCHED_SUFFIX_RESENT.format(form=_form_mark(outcome.form))
    else:
        suffix = ""
    return msg.OUTCOME_MATCHED.format(prefix=prefix, url=outcome.broadcast_url or MISSING_VALUE, suffix=suffix)


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


def _render_package_line(line: ReportPackageLine) -> str:
    return _PACKAGE_TEMPLATES[line.status].format(
        file=line.file_name,
        total=line.slots_total,
        mine=line.slots_mine,
        reason=line.detail,
        version=line.detail,
        supported=SCHEMA_VERSION_SUPPORTED,
        error=line.detail,
    )


def _accepted_line(
    item: AcceptedPackage,
    config: PlanerConfig,
    *,
    is_all_past: bool,
    move: MoveOutcome,
) -> ReportPackageLine:
    path: Path = item.package.path
    if path in move.failed:
        status: PackageLineStatus = PackageLineStatus.ARCHIVE_FAILED if is_all_past else PackageLineStatus.FINISH_FAILED
        return ReportPackageLine(path.name, status, detail=move.failed[path])
    if is_all_past:
        status = PackageLineStatus.ALL_PAST_ARCHIVED if path in move.archived else PackageLineStatus.ALL_PAST_KEPT
        return ReportPackageLine(path.name, status)
    slots_mine: int = sum(1 for slot in item.package.slots if slot.language in config.served_languages)
    status = PackageLineStatus.FINISHED if path in move.finished else PackageLineStatus.ACCEPTED
    return ReportPackageLine(path.name, status, slots_total=item.slots_total, slots_mine=slots_mine)


def _problem_line(problem: PackageProblem) -> ReportPackageLine:
    if problem.reason is PackageErrorReason.UNSUPPORTED_SCHEMA:
        return ReportPackageLine(problem.file.name, PackageLineStatus.UNSUPPORTED_SCHEMA, detail=problem.detail)
    reason_text: str = msg.PACKAGE_REASON_WITH_DETAIL.format(
        reason=msg.PACKAGE_REASON_TEXT[problem.reason.value],
        detail=problem.detail,
    )
    return ReportPackageLine(problem.file.name, PackageLineStatus.DAMAGED, detail=reason_text)


def _skip_text(skipped: SkippedSlot, config: PlanerConfig) -> str:
    if skipped.reason is SkipReason.TOO_LATE:
        return _slot_text(msg.SKIP_TOO_LATE, skipped.slot, minutes=config.min_lead_minutes)
    return _slot_text(msg.SKIP_NO_CHANNEL, skipped.slot)


def _slot_text(template: str, slot: Slot, **extra: object) -> str:
    return template.format(date=slot.date, time=slot.time, language=slot.language, **extra)
