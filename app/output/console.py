"""Консоль после запуска (ТЗ §5.6): перечень блоками сверху вниз — что требует внимания, что куда ушло.

У каждой поверхности свой читатель: консоль — блоки без markdown, отчёт в logs\\ — подробности,
лог — диагностика для разработки. Порядок блоков: ВНИМАНИЕ, ОПУБЛИКОВАЛИ, ИСПРАВИЛИ, КЛЮЧИ СТРИМЕРУ,
УЖЕ СТОЯЛО, НЕ ПУБЛИКОВАЛИ; пустой блок не печатается, нули видны в строке «Итог» (build_totals).
Эфиры группируются по каналу в порядке channels.json; ключ в консоли — только маской, полный — в keys.txt.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from app.core.dates import parse_date
from app.observability.logging_setup import mask_stream_key
from app.output.report import (
    CREATED_OUTCOME_KINDS,
    ERROR_OUTCOME_KINDS,
    MISSING_VALUE,
    FieldChange,
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
    form_reason_text,
    outcome_prefix,
    package_problem_texts,
)
from app.pipeline.plan import ChangedField
from app.ui import messages_ru as msg
from app.version import APP_VERSION

_TITLES: Final[dict[RunMode, str]] = {
    RunMode.FULL: msg.CONSOLE_TITLE,
    RunMode.DRY_RUN: msg.CONSOLE_TITLE_DRY_RUN,
    RunMode.STATUS: msg.CONSOLE_TITLE_STATUS,
}
_TOTALS: Final[dict[RunMode, str]] = {
    RunMode.FULL: msg.CONSOLE_TOTAL,
    RunMode.DRY_RUN: msg.CONSOLE_TOTAL_DRY_RUN,
    RunMode.STATUS: msg.CONSOLE_TOTAL_STATUS,
}
# Тексты эфира правятся по пакету штатно; к норме «возвращают» только настройки — они идут во «Внимание».
_CONTENT_FIELDS: Final[frozenset[str]] = frozenset({ChangedField.TITLE.value, ChangedField.DESCRIPTION.value})
# AMBIGUOUS во «Внимание» приходит предупреждением со ссылками — строка ошибки его бы продублировала.
_ATTENTION_ERROR_KINDS: Final[frozenset[OutcomeKind]] = ERROR_OUTCOME_KINDS - {OutcomeKind.AMBIGUOUS}

LineRender = Callable[[PairOutcome], str]


def render_console(
    report: RunReport,
    *,
    root: Path,
    report_path: Path | None = None,
    log_path: Path | None = None,
    channel_order: Sequence[str] = (),
) -> str:
    """Готовый текст для консоли; печатает main.py. channel_order — имена каналов в порядке channels.json."""
    totals: RunTotals = build_totals(report)
    lines: list[str] = [
        _TITLES[report.mode].format(version=APP_VERSION, generated_at=report.generated_at_text),
        _TOTALS[report.mode].format(
            created=totals.created,
            fixed=totals.fixed,
            matched=totals.matched,
            skipped=totals.skipped,
            errors=totals.errors,
        ),
    ]
    _append_block(lines, _rule(msg.CONSOLE_BLOCK_ATTENTION), _attention_lines(report))
    for title, body in _broadcast_blocks(report, totals, channel_order):
        _append_block(lines, title, body)
    paths: list[str] = _path_lines(report, root, report_path, log_path)
    if paths:
        lines.append("")
        lines.extend(paths)
    return "\n".join(lines)


def _append_block(lines: list[str], header: str, body: list[str]) -> None:
    """Пустой блок не печатается совсем: нули и так видны в «Итоге»."""
    if not body:
        return
    lines.append("")
    lines.append(header)
    lines.extend(body)


def _rule(title: str, count: int | None = None) -> str:
    """Разделитель фиксированной ширины с названием блока (и счётчиком) посередине."""
    text: str = title if count is None else msg.CONSOLE_BLOCK_COUNTED.format(title=title, count=count)
    return msg.CONSOLE_RULE_TITLE.format(title=text).center(msg.CONSOLE_RULE_WIDTH, msg.CONSOLE_RULE_CHAR)


def _broadcast_blocks(
    report: RunReport,
    totals: RunTotals,
    channel_order: Sequence[str],
) -> list[tuple[str, list[str]]]:
    """Блоки эфиров в порядке вывода; --status — только УЖЕ СТОЯЛО, dry-run — без ключей."""
    matched: tuple[str, list[str]] = (
        _rule(msg.CONSOLE_BLOCK_MATCHED, totals.matched),
        _channel_lines(_of_kinds(report, frozenset({OutcomeKind.MATCHED})), channel_order, _broadcast_line),
    )
    if report.mode is RunMode.STATUS:
        return [matched]
    is_dry_run: bool = report.mode is RunMode.DRY_RUN
    blocks: list[tuple[str, list[str]]] = [
        (
            _rule(msg.CONSOLE_BLOCK_CREATED_DRY_RUN if is_dry_run else msg.CONSOLE_BLOCK_CREATED, totals.created),
            _channel_lines(_of_kinds(report, CREATED_OUTCOME_KINDS), channel_order, _broadcast_line),
        ),
        (
            _rule(msg.CONSOLE_BLOCK_FIXED_DRY_RUN if is_dry_run else msg.CONSOLE_BLOCK_FIXED, totals.fixed),
            _channel_lines(
                _of_kinds(report, frozenset({OutcomeKind.FIXED})),
                channel_order,
                _fix_planned_line if is_dry_run else _fixed_line,
            ),
        ),
    ]
    if not is_dry_run:
        blocks.append(_keys_block(report, channel_order))
    blocks.append(matched)
    blocks.append((_rule(msg.CONSOLE_BLOCK_SKIPPED, totals.skipped), _skipped_lines(report.skipped)))
    return blocks


def _of_kinds(report: RunReport, kinds: frozenset[OutcomeKind]) -> list[PairOutcome]:
    return [outcome for outcome in report.outcomes if outcome.kind in kinds and outcome.date is not None]


def _keys_block(report: RunReport, channel_order: Sequence[str]) -> tuple[str, list[str]]:
    """Все ключи, которые в этом запуске должны были дойти до стримера, — и дошедшие, и нет."""
    outcomes: list[PairOutcome] = [
        outcome for outcome in report.outcomes if outcome.form is not None and outcome.date is not None
    ]
    body: list[str] = _channel_lines(outcomes, channel_order, _key_line)
    if body and report.keys_file_path:
        body.append(msg.CONSOLE_KEYS_FILE_NOTE.format(path=report.keys_file_path))
    return _rule(msg.CONSOLE_BLOCK_KEYS, len(outcomes)), body


def _channel_lines(outcomes: list[PairOutcome], channel_order: Sequence[str], render: LineRender) -> list[str]:
    """Шапка канала один раз на группу; каналы — в порядке channels.json, внутри — по дате и времени."""
    ranks: dict[str, int] = {name: index for index, name in enumerate(channel_order)}
    for outcome in outcomes:
        ranks.setdefault(outcome.account_name, len(ranks))
    ordered: list[PairOutcome] = sorted(
        outcomes,
        key=lambda outcome: (ranks[outcome.account_name], parse_date(outcome.date or ""), outcome.time or ""),
    )
    lines: list[str] = []
    current: str | None = None
    for outcome in ordered:
        if outcome.account_name != current:
            current = outcome.account_name
            lines.append(_channel_header(outcome))
        lines.append(render(outcome))
    return lines


def _channel_header(outcome: PairOutcome) -> str:
    if not outcome.google_account:
        return msg.CONSOLE_CHANNEL_GROUP_NO_ACCOUNT.format(account_name=outcome.account_name)
    return msg.CONSOLE_CHANNEL_GROUP.format(account_name=outcome.account_name, google_account=outcome.google_account)


def _broadcast_line(outcome: PairOutcome) -> str:
    return msg.CONSOLE_BROADCAST_LINE.format(
        date=outcome.date,
        time=outcome.time,
        language=outcome.language,
        title=outcome.title or MISSING_VALUE,
    )


def _fixed_line(outcome: PairOutcome) -> str:
    return msg.CONSOLE_FIXED_LINE.format(
        date=outcome.date,
        time=outcome.time,
        language=outcome.language,
        title=outcome.title or MISSING_VALUE,
        what=changed_fields_text(outcome),
    )


def _fix_planned_line(outcome: PairOutcome) -> str:
    return msg.CONSOLE_FIX_PLANNED_LINE.format(
        date=outcome.date,
        time=outcome.time,
        language=outcome.language,
        title=outcome.title or MISSING_VALUE,
        what=changed_fields_text(outcome),
    )


def _key_line(outcome: PairOutcome) -> str:
    """Ключ — только маской (как в логе): полный лежит в keys.txt."""
    state: str = (
        msg.CONSOLE_KEY_SENT
        if outcome.form is FormState.SENT
        else msg.CONSOLE_KEY_FAILED.format(reason=form_reason_text(outcome.form_error))
    )
    return msg.CONSOLE_KEY_LINE.format(
        date=outcome.date,
        time=outcome.time,
        language=outcome.language,
        key=mask_stream_key(outcome.stream_key),
        state=state,
    )


def _skipped_lines(skipped: list[SkippedLine]) -> list[str]:
    """По причине, а не по каналу: у этих слотов канала нет. Порядок строк — как в отчёте."""
    lines: list[str] = []
    for header, group in _skip_groups(skipped):
        lines.append(header)
        lines.extend(
            msg.CONSOLE_BROADCAST_LINE.format(date=line.date, time=line.time, language=line.language, title=line.title)
            for line in group
        )
    return lines


def _skip_groups(skipped: list[SkippedLine]) -> list[tuple[str, list[SkippedLine]]]:
    groups: list[tuple[str, list[SkippedLine]]] = []
    past: list[SkippedLine] = [line for line in skipped if line.kind is SkipKind.PAST]
    if past:
        groups.append((msg.CONSOLE_SKIP_GROUP_PAST, past))
    too_late: list[SkippedLine] = [line for line in skipped if line.kind is SkipKind.TOO_LATE]
    if too_late:
        groups.append((msg.CONSOLE_SKIP_GROUP_TOO_LATE.format(minutes=too_late[0].minutes), too_late))
    no_channel: list[SkippedLine] = [line for line in skipped if line.kind is SkipKind.NO_CHANNEL]
    for language in sorted({line.language for line in no_channel}):
        groups.append(
            (
                msg.CONSOLE_SKIP_GROUP_NO_CHANNEL.format(language=language),
                [line for line in no_channel if line.language == language],
            )
        )
    return groups


def _attention_lines(report: RunReport) -> list[str]:
    """Всё, что требует внимания владельца, полным текстом; постоянные особенности площадки — только в отчёте."""
    lines: list[str] = [
        msg.CONSOLE_ATTENTION_ERROR.format(text=text) for text in error_texts(report, kinds=_ATTENTION_ERROR_KINDS)
    ]
    lines.extend(
        msg.CONSOLE_ATTENTION_NOT_DELIVERED.format(prefix=outcome_prefix(outcome), reason=form_reason_text(outcome.form_error))
        for outcome in report.outcomes
        if outcome.form is FormState.FAILED
    )
    lines.extend(msg.CONSOLE_ATTENTION_PACKAGE.format(text=text) for text in package_problem_texts(report))
    if report.notice:
        lines.append(msg.CONSOLE_ATTENTION_TEXT.format(text=report.notice))
    lines.extend(_restored_lines(report))
    lines.extend(msg.CONSOLE_ATTENTION_TEXT.format(text=text) for text in report.run_warnings)
    return lines


def _restored_lines(report: RunReport) -> list[str]:
    """Настройки, которые планер вернул к пакету (или вернёт в dry-run): видимость, категория, метка."""
    is_dry_run: bool = report.mode is RunMode.DRY_RUN
    return [
        _restored_line(outcome, change, is_dry_run=is_dry_run)
        for outcome in report.outcomes
        if outcome.kind not in ERROR_OUTCOME_KINDS
        for change in outcome.field_changes
        if change.name not in _CONTENT_FIELDS
    ]


def _restored_line(outcome: PairOutcome, change: FieldChange, *, is_dry_run: bool) -> str:
    is_before_known: bool = change.before != MISSING_VALUE
    if is_dry_run:
        template: str = (
            msg.CONSOLE_ATTENTION_RESTORE_PLANNED if is_before_known else msg.CONSOLE_ATTENTION_RESTORE_PLANNED_UNKNOWN
        )
    else:
        template = msg.CONSOLE_ATTENTION_RESTORED if is_before_known else msg.CONSOLE_ATTENTION_RESTORED_UNKNOWN
    return template.format(
        prefix=outcome_prefix(outcome),
        field=msg.CHANGED_FIELD_TEXT[change.name],
        before=change.before,
        after=change.after,
    )


def _path_lines(report: RunReport, root: Path, report_path: Path | None, log_path: Path | None) -> list[str]:
    entries: tuple[tuple[str, str | None], ...] = (
        (msg.CONSOLE_LABEL_KEYS, report.keys_file_path),
        (msg.CONSOLE_LABEL_REPORT, display_path(root, report_path)),
        (msg.CONSOLE_LABEL_LOG, display_path(root, log_path)),
    )
    return [msg.CONSOLE_PATH.format(label=label, path=path) for label, path in entries if path]
