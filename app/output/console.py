"""Консоль после запуска (ТЗ §5.6): что произошло и куда смотреть.

У каждой поверхности свой читатель: консоль — короткая сводка без markdown, отчёт в logs\\ —
подробности, лог — диагностика для разработки. Счётчики — из build_totals, те же, что в «Итоге» отчёта.
Подробности у счётчика — только у ненулевого; ошибки и предупреждения — всегда полным текстом.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Final

from app.output.report import (
    CREATED_OUTCOME_KINDS,
    ERROR_OUTCOME_KINDS,
    FormState,
    OutcomeKind,
    PairOutcome,
    RunMode,
    RunReport,
    RunTotals,
    SkipKind,
    SkippedLine,
    build_totals,
    changed_fields_text,
    display_path,
    error_texts,
    form_mark,
    outcome_prefix,
    package_problem_texts,
)
from app.ui import messages_ru as msg

SINGLE_ITEM: Final[int] = 1   # у счётчика из одного исхода подробность — сам исход
_TITLES: Final[dict[RunMode, str]] = {
    RunMode.FULL: msg.CONSOLE_TITLE,
    RunMode.DRY_RUN: msg.CONSOLE_TITLE_DRY_RUN,
    RunMode.STATUS: msg.CONSOLE_TITLE_STATUS,
}


def render_console(
    report: RunReport,
    *,
    root: Path,
    report_path: Path | None = None,
    log_path: Path | None = None,
) -> str:
    """Готовый текст для консоли; печатает main.py."""
    totals: RunTotals = build_totals(report)
    lines: list[str] = [_TITLES[report.mode].format(generated_at=report.generated_at_text), ""]
    lines.extend(_counter_lines(report, totals))
    _append_block(lines, _attention_lines(report))
    _append_block(lines, _path_lines(report, root, report_path, log_path))
    return "\n".join(lines)


def _append_block(lines: list[str], block: list[str]) -> None:
    if block:
        lines.append("")
        lines.extend(block)


def _counter(label: str, count: int, detail: str = "") -> str:
    """Нулевой счётчик печатается (раздел проверен), но без подробностей."""
    return msg.CONSOLE_COUNTER.format(label=label, count=count, detail=detail if count else "").rstrip()


def _counter_lines(report: RunReport, totals: RunTotals) -> list[str]:
    if report.mode is RunMode.STATUS:
        return [
            _counter(msg.CONSOLE_LABEL_SCHEDULED, totals.matched, _single_detail(report, frozenset({OutcomeKind.MATCHED}), _prefix_only)),
            _counter(msg.CONSOLE_LABEL_ERRORS, totals.errors),
        ]
    is_dry_run: bool = report.mode is RunMode.DRY_RUN
    return [
        _counter(msg.CONSOLE_LABEL_PACKAGES, totals.packages, _packages_detail(totals)),
        _counter(
            msg.CONSOLE_LABEL_CREATE_PLANNED if is_dry_run else msg.CONSOLE_LABEL_CREATED,
            totals.created,
            _created_detail(report, totals, is_dry_run=is_dry_run),
        ),
        _counter(
            msg.CONSOLE_LABEL_FIX_PLANNED if is_dry_run else msg.CONSOLE_LABEL_FIXED,
            totals.fixed,
            _single_detail(report, frozenset({OutcomeKind.FIXED}), _fix_planned_detail if is_dry_run else _fixed_detail),
        ),
        _counter(msg.CONSOLE_LABEL_MATCHED, totals.matched, _single_detail(report, frozenset({OutcomeKind.MATCHED}), _prefix_only)),
        _counter(msg.CONSOLE_LABEL_SKIPPED, totals.skipped, _skipped_detail(report.skipped)),
        _counter(msg.CONSOLE_LABEL_ERRORS, totals.errors),
    ]


def _packages_detail(totals: RunTotals) -> str:
    parts: list[str] = [msg.CONSOLE_PACKAGES_DETAIL.format(total=totals.slots_total, mine=totals.slots_mine)]
    if totals.packages_unreadable:
        parts.append(msg.CONSOLE_PACKAGES_UNREADABLE.format(count=totals.packages_unreadable))
    return msg.CONSOLE_DETAIL_JOINER.join(parts)


def _created_detail(report: RunReport, totals: RunTotals, *, is_dry_run: bool) -> str:
    if totals.created == SINGLE_ITEM:
        return _single_detail(report, CREATED_OUTCOME_KINDS, _prefix_only if is_dry_run else _created_one_detail)
    if is_dry_run:
        return ""
    return msg.CONSOLE_FORMS_SENT.format(sent=totals.form_sent, total=totals.created)


def _single_detail(report: RunReport, kinds: frozenset[OutcomeKind], render: Callable[[PairOutcome], str]) -> str:
    """Подробность — сам исход, если он один; при нескольких подробности в отчёте."""
    matching: list[PairOutcome] = [outcome for outcome in report.outcomes if outcome.kind in kinds]
    return render(matching[0]) if len(matching) == SINGLE_ITEM else ""


def _prefix_only(outcome: PairOutcome) -> str:
    return outcome_prefix(outcome)


def _created_one_detail(outcome: PairOutcome) -> str:
    if outcome.form is None:
        return outcome_prefix(outcome)
    mark: str = msg.FORM_MARK_SENT if outcome.form is FormState.SENT else msg.CONSOLE_FORM_NOT_SENT
    return msg.CONSOLE_DETAIL_JOINER.join((outcome_prefix(outcome), mark))


def _fixed_detail(outcome: PairOutcome) -> str:
    return msg.CONSOLE_FIXED_DETAIL.format(prefix=outcome_prefix(outcome), what=changed_fields_text(outcome))


def _fix_planned_detail(outcome: PairOutcome) -> str:
    return msg.CONSOLE_FIX_PLANNED_DETAIL.format(prefix=outcome_prefix(outcome), what=changed_fields_text(outcome))


def _skipped_detail(skipped: list[SkippedLine]) -> str:
    """Пропуски сгруппированы по причине: прошло, поздно, нет канала для языков."""
    parts: list[str] = []
    past: int = sum(1 for line in skipped if line.kind is SkipKind.PAST)
    if past:
        parts.append(msg.CONSOLE_SKIP_PAST.format(count=past))
    too_late: list[SkippedLine] = [line for line in skipped if line.kind is SkipKind.TOO_LATE]
    if too_late:
        parts.append(msg.CONSOLE_SKIP_TOO_LATE.format(minutes=too_late[0].minutes, count=len(too_late)))
    no_channel: Counter[str] = Counter(line.language for line in skipped if line.kind is SkipKind.NO_CHANNEL)
    if no_channel:
        # по числу слотов на язык: сумма сходится со счётчиком «пропущено»
        languages: str = msg.CONSOLE_DETAIL_JOINER.join(
            msg.CONSOLE_SKIP_LANGUAGE_COUNT.format(language=language, count=no_channel[language])
            for language in sorted(no_channel)
        )
        parts.append(msg.CONSOLE_SKIP_NO_CHANNEL.format(languages=languages))
    return msg.CONSOLE_SKIP_JOINER.join(parts)


def _attention_lines(report: RunReport) -> list[str]:
    """Всё, что требует внимания владельца, — полным текстом."""
    lines: list[str] = [msg.CONSOLE_ERROR.format(text=text) for text in error_texts(report)]
    lines.extend(
        msg.CONSOLE_FORM_ERROR.format(prefix=outcome_prefix(outcome), mark=form_mark(outcome.form, outcome.form_error))
        for outcome in report.outcomes
        if outcome.form is FormState.FAILED and outcome.kind not in ERROR_OUTCOME_KINDS
    )
    lines.extend(msg.CONSOLE_PACKAGE_ERROR.format(text=text) for text in package_problem_texts(report))
    if report.notice:
        lines.append(msg.CONSOLE_WARNING.format(text=report.notice))
    # только предупреждения запуска: постоянные особенности площадки (report.notes) — в отчёте
    lines.extend(msg.CONSOLE_WARNING.format(text=text) for text in report.run_warnings)
    return lines


def _path_lines(report: RunReport, root: Path, report_path: Path | None, log_path: Path | None) -> list[str]:
    entries: tuple[tuple[str, str | None], ...] = (
        (msg.CONSOLE_LABEL_KEYS, report.keys_file_path),
        (msg.CONSOLE_LABEL_REPORT, display_path(root, report_path)),
        (msg.CONSOLE_LABEL_LOG, display_path(root, log_path)),
    )
    return [msg.CONSOLE_PATH.format(label=label, path=path) for label, path in entries if path]
