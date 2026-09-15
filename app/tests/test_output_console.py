from __future__ import annotations

import random
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.console import render_console
from app.output.report import (
    FormState,
    OutcomeKind,
    PackageLineStatus,
    PairOutcome,
    ReportPackageLine,
    RunMode,
    RunReport,
    RunTotals,
    SkipKind,
    SkippedLine,
    build_totals,
)
from app.paths import PlanerPaths
from app.pipeline.plan import OutcomeError
from app.pipeline.runner import RunOutcome, run
from app.platforms.base import PlatformError
from app.platforms.fake import FakePlatform
from app.tests.conftest import FakeFormSender
from app.ui import messages_ru as msg

ROOT: Path = Path("D:/planer")
SAMPLE_CONSOLE_LINES: int = 13   # заголовок, пустая, 6 счётчиков, строка исхода, пустая, 3 пути
OUTCOME: str = msg.CONSOLE_OUTCOME_INDENT      # строка исхода — в колонке подробностей счётчика
PACKAGE: str = "plan_17-03-2027_18-03-2027_gen11-09-2026-1658.bcast"

# Макет задач 4e/4g/5c: один созданный эфир, три пропуска без канала; живой чат — постоянная
# особенность площадки, в консоль не попадает, поэтому блока «внимание» нет.
SAMPLE_CONSOLE: str = """Планер — 13-09-2026 13:20

  пакеты          1   слотов 4, моих 1
  создано         1   ключ передан в форму: 1 из 1
                      17-03-2027 19:00 ru -> Osvald.X
  исправлено      0
  совпадает       0
  пропущено       3   нет канала: en (2), uk (1)
  ошибок          0

  ключи   keystreams\\keys.txt
  отчёт   logs\\13-09-2026_132051_report.md
  лог     logs\\13-09-2026_132050_planer.log"""


def _created(day: int, form: FormState | None, form_error: str | None = None) -> PairOutcome:
    return PairOutcome(
        OutcomeKind.CREATED, "Osvald.X", f"{day}-03-2027", "19:00", "ru", form=form, form_error=form_error
    )


def _sample_report(**overrides: Any) -> RunReport:
    values: dict[str, Any] = dict(
        mode=RunMode.FULL,
        generated_at_text="13-09-2026 13:20",
        packages=[ReportPackageLine(PACKAGE, PackageLineStatus.ACCEPTED, slots_total=4, slots_mine=1)],
        outcomes=[_created(17, FormState.SENT)],
        skipped=[
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "uk"),
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "en"),
            SkippedLine(SkipKind.NO_CHANNEL, "18-03-2027", "19:00", "en"),
        ],
        warnings=[msg.WARNING_LIVE_CHAT],
        keys_file_path="keystreams\\keys.txt",
    )
    values.update(overrides)
    return RunReport(**values)


def _render(report: RunReport) -> str:
    return render_console(
        report,
        root=ROOT,
        report_path=ROOT / "logs" / "13-09-2026_132051_report.md",
        log_path=ROOT / "logs" / "13-09-2026_132050_planer.log",
    )


def test_full_run_matches_the_layout() -> None:
    text: str = _render(_sample_report())
    assert text.replace("/", "\\") == SAMPLE_CONSOLE
    assert len(text.splitlines()) == SAMPLE_CONSOLE_LINES


def test_platform_notes_never_reach_the_console() -> None:
    """Живой чат и прежний ключ — так устроена площадка: печатаются только в отчёте."""
    run_warning: str = "18-03-2027 20:00 ru -> Osvald.X: обложка не поставлена — forbidden (канал не подтверждён)"
    report: RunReport = _sample_report(warnings=[run_warning, msg.WARNING_LIVE_CHAT, msg.WARNING_KEPT_KEY])
    text: str = _render(report)
    assert f"  внимание: {run_warning}" in text.splitlines()
    assert "живой чат" not in text and "ключ прежний" not in text
    assert text.count("внимание:") == 1


def test_console_has_no_markdown_and_no_icons() -> None:
    text: str = _render(_sample_report(outcomes=[_created(17, FormState.FAILED, "notConfirmed: HTTP 200")]))
    assert "#" not in text and "\n- " not in text
    assert all(icon not in text for icon in ("✅", "❌", "⚠", "→"))


def test_zero_counters_are_printed_without_details() -> None:
    lines: list[str] = _render(_sample_report(outcomes=[], skipped=[], warnings=[])).splitlines()
    assert "  создано         0" in lines
    assert "  пропущено       0" in lines
    assert "  ошибок          0" in lines


def test_outcome_indent_is_the_detail_column() -> None:
    """Отступ строки исхода собран из тех же ширин, что CONSOLE_COUNTER: подробность и исход — одна колонка."""
    counter: str = msg.CONSOLE_COUNTER.format(label=msg.CONSOLE_LABEL_CREATED, count=1, detail="X")
    assert counter.index("X") == len(OUTCOME)


def test_sent_key_is_not_repeated_in_the_broadcast_line() -> None:
    """Норму несёт сводка на строке счётчика; строка эфира без отметки формы."""
    lines: list[str] = _render(_sample_report()).splitlines()
    created: int = lines.index("  создано         1   ключ передан в форму: 1 из 1")
    assert lines[created + 1] == f"{OUTCOME}17-03-2027 19:00 ru -> Osvald.X"
    assert sum("ключ передан в форму" in line for line in lines) == 1


def test_several_created_show_every_broadcast_and_form_summary() -> None:
    report: RunReport = _sample_report(
        outcomes=[_created(17, FormState.SENT), _created(18, FormState.FAILED, "transportFailed: HTTP 503")]
    )
    lines: list[str] = _render(report).splitlines()
    created: int = lines.index("  создано         2   ключ передан в форму: 1 из 2")
    assert lines[created + 1 : created + 3] == [
        f"{OUTCOME}17-03-2027 19:00 ru -> Osvald.X",                          # норма — без отметки
        f"{OUTCOME}18-03-2027 19:00 ru -> Osvald.X, ключ в форму НЕ передан",  # аномалия — с отметкой
    ]
    assert lines[created + 3] == "  исправлено      0"
    text: str = "\n".join(lines)
    assert (
        "  форма: 18-03-2027 19:00 ru -> Osvald.X — ключ в форму НЕ передан — форма недоступна (HTTP 503)"
    ) in text


def test_errors_packages_and_warnings_are_printed_in_full() -> None:
    report: RunReport = _sample_report(
        packages=[
            ReportPackageLine(PACKAGE, PackageLineStatus.ACCEPTED, slots_total=4, slots_mine=1),
            ReportPackageLine("broken.bcast", PackageLineStatus.DAMAGED, detail="не ZIP-архив"),
        ],
        outcomes=[
            PairOutcome(
                OutcomeKind.ERROR,
                "Osvald.X",
                "18-03-2027",
                "20:00",
                "ru",
                error=OutcomeError("youtube", "liveStreamingNotEnabled", "на канале не включены трансляции"),
            )
        ],
        warnings=["18-03-2027 20:00 ru -> Osvald.X: обложка не поставлена — forbidden (канал не подтверждён)"],
    )
    lines: list[str] = _render(report).splitlines()
    assert "  пакеты          2   слотов 4, моих 1, не прочитано 1" in lines
    assert "  ошибок          1" in lines
    assert (
        "  ошибка: 18-03-2027 20:00 ru -> Osvald.X — YouTube: liveStreamingNotEnabled (на канале не включены трансляции)"
    ) in lines
    assert "  пакет: broken.bcast — пакет повреждён: не ZIP-архив; файл не тронут" in lines
    assert "  внимание: 18-03-2027 20:00 ru -> Osvald.X: обложка не поставлена — forbidden (канал не подтверждён)" in lines


def test_skipped_are_grouped_by_reason() -> None:
    report: RunReport = _sample_report(
        skipped=[
            SkippedLine(SkipKind.PAST, "14-03-2027", "19:00", "ru"),
            SkippedLine(SkipKind.TOO_LATE, "16-03-2027", "12:30", "ru", minutes=60),
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "hu"),
        ]
    )
    assert "  пропущено       3   уже прошло: 1; до старта меньше 60 минут: 1; нет канала: hu (1)" in _render(report)


def test_no_channel_skips_are_counted_per_language() -> None:
    """Сумма по языкам сходится со счётчиком: три пропуска — en (2) и uk (1), а не «en, uk»."""
    lines: list[str] = _render(_sample_report()).splitlines()
    assert "  пропущено       3   нет канала: en (2), uk (1)" in lines


def test_dry_run_has_its_own_title_and_labels() -> None:
    report: RunReport = _sample_report(
        mode=RunMode.DRY_RUN,
        outcomes=[
            PairOutcome(OutcomeKind.CREATED, "Osvald.X", "17-03-2027", "19:00", "ru"),
            PairOutcome(OutcomeKind.CREATED, "Oktavian.X", "17-03-2027", "19:00", "uk"),
            PairOutcome(OutcomeKind.FIXED, "Osvald.X", "18-03-2027", "19:00", "ru", changed_fields=("title",)),
        ],
        keys_file_path=None,
    )
    lines: list[str] = _render(report).splitlines()
    assert lines[0] == "Планер — 13-09-2026 13:20 — dry-run: ничего не создано и в форму не отправлено"
    create: int = lines.index("  создать         2")
    assert lines[create + 1 : create + 5] == [
        f"{OUTCOME}17-03-2027 19:00 ru -> Osvald.X",
        f"{OUTCOME}17-03-2027 19:00 uk -> Oktavian.X",
        "  исправить       1",
        f"{OUTCOME}18-03-2027 19:00 ru -> Osvald.X, будет обновлено: название",
    ]
    assert lines[create + 5] == "  совпадает       0"
    assert not any(line.lstrip().startswith(msg.CONSOLE_LABEL_KEYS) for line in lines)


def test_status_counts_scheduled_and_errors_only() -> None:
    report: RunReport = RunReport(
        mode=RunMode.STATUS,
        generated_at_text="13-09-2026 13:20",
        outcomes=[
            PairOutcome(OutcomeKind.MATCHED, "Osvald.X", "17-03-2027", "19:00", "ru", broadcast_url="u1"),
            PairOutcome(OutcomeKind.ERROR, "Test RU", error=OutcomeError("youtube", "quotaExceeded", "квота исчерпана")),
        ],
        keys_file_path="keystreams\\keys.txt",
    )
    lines: list[str] = _render(report).splitlines()
    assert lines[0] == "Планер — 13-09-2026 13:20 — --status: эфиры планера на каналах"
    assert lines[2:5] == ["  запланировано   1", f"{OUTCOME}17-03-2027 19:00 ru -> Osvald.X", "  ошибок          1"]
    assert "  ошибка: Test RU — YouTube: quotaExceeded (квота исчерпана)" in lines


def test_console_and_report_use_the_same_totals(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: Callable[..., PlanerConfig],
    fake_platform: FakePlatform,
    form_sender: FakeFormSender,
    now: datetime,
    rng: random.Random,
) -> None:
    """Production-путь: запуск через runner, числа консоли совпадают с «Итогом» записанного отчёта."""
    make_package(
        planer_paths.bcast_dir,
        slots=[
            make_slot("17-03-2027", "19:00", "uk"),
            make_slot("18-03-2027", "19:00", "ru"),
            make_slot("17-03-2027", "19:00", "hu"),
        ],
    )
    fake_platform.fail_create["18-03-2027_1900_ru"] = PlatformError("forbidden", "нельзя")
    outcome: RunOutcome = run(RunMode.FULL, make_config(), planer_paths, fake_platform, form_sender, now, rng)
    assert outcome.report is not None and outcome.report_path is not None
    totals: RunTotals = build_totals(outcome.report)
    console: str = render_console(outcome.report, root=planer_paths.root, report_path=outcome.report_path)
    report_text: str = outcome.report_path.read_text(encoding="utf-8")
    assert (
        f"Итог: создано {totals.created}, исправлено {totals.fixed}, совпадает {totals.matched}, "
        f"пропущено {totals.skipped}, ошибок {totals.errors}."
    ) in report_text
    for label, count in (
        (msg.CONSOLE_LABEL_CREATED, totals.created),
        (msg.CONSOLE_LABEL_SKIPPED, totals.skipped),
        (msg.CONSOLE_LABEL_ERRORS, totals.errors),
    ):
        assert re.search(rf"^  {label} +{count}\b", console, re.MULTILINE)
    assert (totals.created, totals.skipped, totals.errors) == (1, 1, 1)
    assert f"  отчёт   {Path('logs') / outcome.report_path.name}" in console
