"""Отчёт запуска (ТЗ §5.6): сборка, текст, файл reports\\report_*.md.

Этап 2a: разделы «Создано», «Исправлено», «Уже запланировано», «Ошибки» пусты —
их наполняет сверка (2b). До 2b в отчёте есть временный раздел «К сверке с YouTube».
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
from app.pipeline.selection import Selection, SkippedSlot, SkipReason, SlotChannelPair
from app.ui import messages_ru as msg

REPORT_FILE_TEMPLATE: Final[str] = "report_{stamp}.md"
REPORT_ENCODING: Final[str] = "utf-8"


class PackageLineStatus(str, Enum):
    ACCEPTED = "accepted"
    DAMAGED = "damaged"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    ALL_PAST_ARCHIVED = "all_past_archived"
    ALL_PAST_KEPT = "all_past_kept"      # dry-run: не перенесён
    ARCHIVE_FAILED = "archive_failed"


_PACKAGE_TEMPLATES: Final[dict[PackageLineStatus, str]] = {
    PackageLineStatus.ACCEPTED: msg.PACKAGE_ACCEPTED,
    PackageLineStatus.DAMAGED: msg.PACKAGE_DAMAGED,
    PackageLineStatus.UNSUPPORTED_SCHEMA: msg.PACKAGE_UNSUPPORTED_SCHEMA,
    PackageLineStatus.ALL_PAST_ARCHIVED: msg.PACKAGE_ALL_PAST_ARCHIVED,
    PackageLineStatus.ALL_PAST_KEPT: msg.PACKAGE_ALL_PAST_KEPT,
    PackageLineStatus.ARCHIVE_FAILED: msg.PACKAGE_ARCHIVE_FAILED,
}


@dataclass(frozen=True)
class ReportPackageLine:
    file_name: str
    status: PackageLineStatus
    slots_total: int = 0
    slots_mine: int = 0
    detail: str = ""   # причина повреждения, версия схемы или ошибка переноса


@dataclass(frozen=True)
class RunReport:
    generated_at_text: str
    owner: str
    packages: list[ReportPackageLine]
    created: list[str]
    fixed: list[str]
    matched: list[str]
    skipped: list[str]
    errors: list[str]
    keys_file_path: str | None = None
    pairs_to_reconcile: list[str] = field(default_factory=list)   # временно, до 2b


@dataclass(frozen=True)
class ArchiveOutcome:
    """Что стало с пакетами «все слоты в прошлом»: перенесены, не перенесены (ошибка)."""

    moved: frozenset[Path] = frozenset()
    failed: dict[Path, str] = field(default_factory=dict)


def build_run_report(
    *,
    scan: InboxScan,
    selection: Selection,
    config: PlanerConfig,
    archive_outcome: ArchiveOutcome,
    generated_at_text: str,
) -> RunReport:
    return RunReport(
        generated_at_text=generated_at_text,
        owner=config.owner,
        packages=_package_lines(scan, config, archive_outcome),
        created=[],
        fixed=[],
        matched=[],
        skipped=_skipped_lines(scan, selection, config),
        errors=[],
        keys_file_path=None,
        pairs_to_reconcile=[_pair_line(pair) for pair in selection.pairs],
    )


def render_report(report: RunReport) -> str:
    """Структура ТЗ §5.6; пустой раздел — заголовок без строк."""
    lines: list[str] = [
        msg.REPORT_TITLE.format(generated_at=report.generated_at_text, owner=report.owner),
        "",
    ]
    _append_section(lines, msg.REPORT_SECTION_PACKAGES, [_render_package_line(line) for line in report.packages])
    _append_section(lines, msg.REPORT_SECTION_CREATED.format(count=len(report.created)), report.created)
    _append_section(lines, msg.REPORT_SECTION_FIXED.format(count=len(report.fixed)), report.fixed)
    _append_section(lines, msg.REPORT_SECTION_MATCHED.format(count=len(report.matched)), report.matched)
    if report.pairs_to_reconcile:
        _append_section(
            lines,
            msg.REPORT_SECTION_PENDING.format(count=len(report.pairs_to_reconcile)),
            report.pairs_to_reconcile,
        )
    _append_section(lines, msg.REPORT_SECTION_SKIPPED, report.skipped)
    _append_section(lines, msg.REPORT_SECTION_ERRORS, report.errors)
    lines.append(_render_total(report))
    return "\n".join(lines) + "\n"


def write_report(paths: PlanerPaths, text: str, now_local: datetime) -> Path:
    report_path: Path = paths.reports_dir / REPORT_FILE_TEMPLATE.format(
        stamp=now_local.strftime(REPORT_STAMP_FORMAT)
    )
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding=REPORT_ENCODING)
    return report_path


def _append_section(lines: list[str], header: str, body: list[str]) -> None:
    lines.append(header)
    lines.extend(body)
    lines.append("")


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


def _render_total(report: RunReport) -> str:
    pending: str = (
        msg.REPORT_TOTAL_PENDING.format(count=len(report.pairs_to_reconcile))
        if report.pairs_to_reconcile
        else ""
    )
    keys_file: str = (
        msg.REPORT_TOTAL_KEYS_FILE.format(path=report.keys_file_path) if report.keys_file_path else ""
    )
    return msg.REPORT_TOTAL.format(
        created=len(report.created),
        fixed=len(report.fixed),
        matched=len(report.matched),
        skipped=len(report.skipped),
        errors=len(report.errors),
        pending=pending,
        keys_file=keys_file,
    )


def _package_lines(scan: InboxScan, config: PlanerConfig, outcome: ArchiveOutcome) -> list[ReportPackageLine]:
    all_past: set[Path] = {package.path for package in scan.all_past_packages}
    lines: list[ReportPackageLine] = [
        _accepted_line(item, config, is_all_past=item.package.path in all_past, outcome=outcome)
        for item in scan.packages
    ]
    lines.extend(_problem_line(problem) for problem in scan.problems)
    return lines


def _accepted_line(
    item: AcceptedPackage,
    config: PlanerConfig,
    *,
    is_all_past: bool,
    outcome: ArchiveOutcome,
) -> ReportPackageLine:
    path: Path = item.package.path
    if is_all_past:
        if path in outcome.failed:
            return ReportPackageLine(path.name, PackageLineStatus.ARCHIVE_FAILED, detail=outcome.failed[path])
        status: PackageLineStatus = (
            PackageLineStatus.ALL_PAST_ARCHIVED if path in outcome.moved else PackageLineStatus.ALL_PAST_KEPT
        )
        return ReportPackageLine(path.name, status)
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


def _skipped_lines(scan: InboxScan, selection: Selection, config: PlanerConfig) -> list[str]:
    """«Уже прошло» — только слоты моих языков; плюс too_late / no_channel из отбора (§7.2)."""
    entries: list[tuple[Slot, str]] = [
        (slot, _slot_text(msg.SKIP_PAST, slot))
        for slot in scan.past_slots
        if slot.language in config.served_languages
    ]
    entries.extend((skipped.slot, _skip_text(skipped, config)) for skipped in selection.skipped)
    entries.sort(key=lambda entry: slot_order_key(entry[0]))
    return [text for _, text in entries]


def _skip_text(skipped: SkippedSlot, config: PlanerConfig) -> str:
    if skipped.reason is SkipReason.TOO_LATE:
        return _slot_text(msg.SKIP_TOO_LATE, skipped.slot, minutes=config.min_lead_minutes)
    return _slot_text(msg.SKIP_NO_CHANNEL, skipped.slot)


def _slot_text(template: str, slot: Slot, **extra: object) -> str:
    return template.format(date=slot.date, time=slot.time, language=slot.language, **extra)


def _pair_line(pair: SlotChannelPair) -> str:
    return _slot_text(
        msg.PAIR_TO_RECONCILE,
        pair.slot,
        account_name=pair.channel.account_name,
        channel_id=pair.channel.id,
    )
