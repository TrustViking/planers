from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import (
    ArchiveOutcome,
    PackageLineStatus,
    ReportPackageLine,
    RunReport,
    build_run_report,
    render_report,
    write_report,
)
from app.package.inbox import InboxScan, scan_inbox
from app.paths import PlanerPaths
from app.pipeline.selection import Selection, select_pairs

# Пример из ТЗ §5.6 с согласованными счётчиками (в ТЗ строки разделов даны выборочно).
TZ_SAMPLE_REPORT: str = """# Планер — отчёт 13-09-2026 12:00, владелец: Иван

## Пакеты
- plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast — принят, слотов 24, из них под мои языки 9
- plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast — все слоты в прошлом, перенесён в inbox\\archive

## Создано (2)
- 16-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ✅
- 18-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ❌ (повторю в следующий запуск)

## Исправлено (1)
- 17-09-2026 19:00 uk → Іван UA — на YouTube было другое описание; обновлено. Ключ и ссылка прежние, форма не переотправлялась

## Уже запланировано, совпадает (1)
- 16-09-2026 21:00 ru → Иван RU — https://www.youtube.com/watch?v=def456

## Пропущено
- 14-09-2026 19:00 uk — уже прошло
- 15-09-2026 19:00 en — нет канала для языка en
- 13-09-2026 12:30 uk — до старта меньше 60 минут

## Ошибки
- 19-09-2026 19:00 uk → Іван UA — YouTube: liveStreamingNotEnabled (на канале не включены трансляции)

Итог: создано 2, исправлено 1, копий 1, пропущено 3, ошибок 1. Файл ключей: out\\keys.txt
"""

EMPTY_REPORT: str = """# Планер — отчёт 16-03-2027 12:00, владелец: Тест

## Пакеты

## Создано (0)

## Исправлено (0)

## Уже запланировано, совпадает (0)

## Пропущено

## Ошибки

Итог: создано 0, исправлено 0, копий 0, пропущено 0, ошибок 0.
"""


def test_render_matches_tz_structure() -> None:
    report: RunReport = RunReport(
        generated_at_text="13-09-2026 12:00",
        owner="Иван",
        packages=[
            ReportPackageLine(
                "plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast",
                PackageLineStatus.ACCEPTED,
                slots_total=24,
                slots_mine=9,
            ),
            ReportPackageLine("plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast", PackageLineStatus.ALL_PAST_ARCHIVED),
        ],
        created=[
            "- 16-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ✅",
            "- 18-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ❌ (повторю в следующий запуск)",
        ],
        fixed=[
            "- 17-09-2026 19:00 uk → Іван UA — на YouTube было другое описание; обновлено. "
            "Ключ и ссылка прежние, форма не переотправлялась"
        ],
        matched=["- 16-09-2026 21:00 ru → Иван RU — https://www.youtube.com/watch?v=def456"],
        skipped=[
            "- 14-09-2026 19:00 uk — уже прошло",
            "- 15-09-2026 19:00 en — нет канала для языка en",
            "- 13-09-2026 12:30 uk — до старта меньше 60 минут",
        ],
        errors=["- 19-09-2026 19:00 uk → Іван UA — YouTube: liveStreamingNotEnabled (на канале не включены трансляции)"],
        keys_file_path="out\\keys.txt",
    )
    assert render_report(report) == TZ_SAMPLE_REPORT


def test_empty_sections_render_with_zero() -> None:
    report: RunReport = RunReport("16-03-2027 12:00", "Тест", [], [], [], [], [], [])
    assert render_report(report) == EMPTY_REPORT


def test_pending_pairs_add_temporary_section_and_total() -> None:
    report: RunReport = RunReport(
        "16-03-2027 12:00",
        "Тест",
        [],
        [],
        [],
        [],
        [],
        [],
        pairs_to_reconcile=["- 17-03-2027 19:00 uk → Test UA (yt_ua)"],
    )
    text: str = render_report(report)
    assert "## К сверке с YouTube (1)" in text
    assert "- 17-03-2027 19:00 uk → Test UA (yt_ua)\n" in text
    assert text.endswith("ошибок 0; к сверке: 1 пар.\n")


def test_build_run_report_from_scan_and_selection(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    make_package(
        planer_paths.inbox_dir,
        file_name="plan.bcast",
        slots=[
            make_slot("15-03-2027", "19:00", "uk"),
            make_slot("15-03-2027", "19:00", "hu"),
            make_slot("16-03-2027", "12:30", "ru"),
            make_slot("17-03-2027", "19:00", "hu"),
            make_slot("17-03-2027", "19:00", "uk"),
        ],
    )
    config: PlanerConfig = make_config()
    scan: InboxScan = scan_inbox(planer_paths, now)
    selection: Selection = select_pairs(scan.slot_map, config, now)
    report: RunReport = build_run_report(
        scan=scan,
        selection=selection,
        config=config,
        archive_outcome=ArchiveOutcome(),
        generated_at_text="16-03-2027 12:00",
    )
    assert report.packages == [ReportPackageLine("plan.bcast", PackageLineStatus.ACCEPTED, slots_total=5, slots_mine=3)]
    assert report.skipped == [
        "- 15-03-2027 19:00 uk — уже прошло",
        "- 16-03-2027 12:30 ru — до старта меньше 60 минут",
        "- 17-03-2027 19:00 hu — нет канала для языка hu",
    ]
    assert report.pairs_to_reconcile == ["- 17-03-2027 19:00 uk → Account yt_ua (yt_ua)"]


def test_write_report_uses_stamped_name(planer_paths: PlanerPaths) -> None:
    path: Path = write_report(planer_paths, "текст\n", datetime(2027, 3, 16, 12, 0, 5))
    assert path == planer_paths.reports_dir / "report_16-03-2027_120005.md"
    assert path.read_text(encoding="utf-8") == "текст\n"
