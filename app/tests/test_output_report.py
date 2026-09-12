from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import (
    FormState,
    OrphanLine,
    OutcomeError,
    OutcomeKind,
    PackageLineStatus,
    PairOutcome,
    ReportPackageLine,
    RunMode,
    RunReport,
    build_package_lines,
    build_skipped_lines,
    render_report,
    write_report,
)
from app.package.promo import PromoScan, scan_promo
from app.paths import PlanerPaths
from app.pipeline.selection import Selection, select_pairs

# Пример из ТЗ §5.6 с согласованными счётчиками (в ТЗ строки разделов даны выборочно).
TZ_SAMPLE_REPORT: str = """# Планер — отчёт 13-09-2026 12:00, владелец: Иван

## Пакеты
- plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast — принят, слотов 24, из них под мои языки 9
- plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast — все слоты в прошлом, ничего из него не планируется

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

Итог: создано 2, исправлено 1, копий 1, пропущено 3, ошибок 1. Файл ключей: keystreams\\keys.txt
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

DRY_RUN_REPORT: str = """# Планер — отчёт 16-03-2027 12:00, владелец: Тест (dry-run)
⚠ Площадка — заглушка

## Пакеты

## Создано (1)
- 17-03-2027 19:00 uk → Test UA — эфира нет, будет создан — не выполнено (dry-run)

## Исправлено (0)

## Уже запланировано, совпадает (0)

## Пропущено

## Ошибки

Итог: создано 1, исправлено 0, копий 0, пропущено 0, ошибок 0.
"""

STATUS_REPORT: str = """# Планер — отчёт 16-03-2027 12:00, владелец: Тест

## Запланировано на каналах (1)
- 17-03-2027 19:00 uk → Test UA — https://www.youtube.com/watch?v=abc

## Ошибки
- Test RU — YouTube: quotaExceeded (квота исчерпана)

Итог: запланировано 1, ошибок 1. Файл ключей: keystreams\\keys.txt
"""


def _slot_outcome(kind: OutcomeKind, **overrides: Any) -> PairOutcome:
    values: dict[str, Any] = dict(account_name="Test UA", date="17-03-2027", time="19:00", language="uk")
    values.update(overrides)
    return PairOutcome(kind, **values)


def test_render_matches_tz_structure() -> None:
    report: RunReport = RunReport(
        mode=RunMode.FULL,
        generated_at_text="13-09-2026 12:00",
        owner="Иван",
        packages=[
            ReportPackageLine(
                "plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast",
                PackageLineStatus.ACCEPTED,
                slots_total=24,
                slots_mine=9,
            ),
            ReportPackageLine(
                "plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast",
                PackageLineStatus.ALL_PAST,
            ),
        ],
        outcomes=[
            PairOutcome(OutcomeKind.CREATED, "Іван UA", "16-09-2026", "19:00", "uk", form=FormState.SENT),
            PairOutcome(OutcomeKind.CREATED, "Іван UA", "18-09-2026", "19:00", "uk", form=FormState.FAILED),
            PairOutcome(OutcomeKind.FIXED, "Іван UA", "17-09-2026", "19:00", "uk", changed_fields=("description",)),
            PairOutcome(
                OutcomeKind.MATCHED,
                "Иван RU",
                "16-09-2026",
                "21:00",
                "ru",
                broadcast_url="https://www.youtube.com/watch?v=def456",
            ),
            PairOutcome(
                OutcomeKind.ERROR,
                "Іван UA",
                "19-09-2026",
                "19:00",
                "uk",
                error=OutcomeError("youtube", "liveStreamingNotEnabled", "на канале не включены трансляции"),
            ),
        ],
        skipped=[
            "- 14-09-2026 19:00 uk — уже прошло",
            "- 15-09-2026 19:00 en — нет канала для языка en",
            "- 13-09-2026 12:30 uk — до старта меньше 60 минут",
        ],
        keys_file_path="keystreams\\keys.txt",
    )
    assert render_report(report) == TZ_SAMPLE_REPORT


def test_empty_sections_render_with_zero() -> None:
    assert render_report(RunReport(RunMode.FULL, "16-03-2027 12:00", "Тест")) == EMPTY_REPORT


def test_dry_run_marks_title_and_every_outcome() -> None:
    report: RunReport = RunReport(
        RunMode.DRY_RUN,
        "16-03-2027 12:00",
        "Тест",
        outcomes=[_slot_outcome(OutcomeKind.CREATED)],
        notice="Площадка — заглушка",
    )
    assert render_report(report) == DRY_RUN_REPORT


def test_status_report_structure() -> None:
    report: RunReport = RunReport(
        RunMode.STATUS,
        "16-03-2027 12:00",
        "Тест",
        outcomes=[
            _slot_outcome(OutcomeKind.MATCHED, broadcast_url="https://www.youtube.com/watch?v=abc"),
            PairOutcome(OutcomeKind.ERROR, "Test RU", error=OutcomeError("youtube", "quotaExceeded", "квота исчерпана")),
        ],
        keys_file_path="keystreams\\keys.txt",
    )
    assert render_report(report) == STATUS_REPORT


def test_orphans_section_appears_after_matched() -> None:
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        "Тест",
        orphans=[OrphanLine("19-03-2027", "12:00", "uk", "Test UA", "https://www.youtube.com/watch?v=old")],
    )
    text: str = render_report(report)
    assert (
        "## Уже запланировано, совпадает (0)\n\n"
        "## Перенесён или отменён? (1)\n"
        "- 19-03-2027 12:00 uk → Test UA — https://www.youtube.com/watch?v=old — эфир не удалён\n"
    ) in text


def test_rebind_resend_ambiguous_and_planer_error_texts() -> None:
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        "Тест",
        outcomes=[
            _slot_outcome(OutcomeKind.MATCHED, broadcast_url="u1", rebind=True, form=FormState.WAITING),
            _slot_outcome(OutcomeKind.MATCHED, broadcast_url="u2", form=FormState.SENT),
            _slot_outcome(OutcomeKind.FIXED, changed_fields=("title", "description"), rebind=True, form=FormState.SENT),
            _slot_outcome(OutcomeKind.CREATED, recreated=True, form=FormState.SENT),
            _slot_outcome(OutcomeKind.AMBIGUOUS),
            _slot_outcome(OutcomeKind.ERROR, error=OutcomeError("planer", "noBoundStream")),
        ],
    )
    text: str = render_report(report)
    assert "— u1; эфира не было в журнале — ключ взят с площадки, форма ⏳ отправка появится на этапе 4\n" in text
    assert "— u2; форма ✅ (повторная отправка)\n" in text
    assert "другое название и описание; обновлено. Эфира не было в журнале — ключ взят с площадки, форма ✅" in text
    assert "эфира на YouTube не было (удалён?), создан заново, ключ получен, форма ✅" in text
    assert "несколько эфиров на эту минуту без маркера планера" in text
    assert "у найденного эфира нет привязанного потока" in text
    assert text.endswith("ошибок 2.\n")


def test_package_and_skipped_lines_from_scan_and_selection(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    make_package(
        planer_paths.promo_dir,
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
    scan: PromoScan = scan_promo(planer_paths, now)
    selection: Selection = select_pairs(scan.slot_map, config, now)
    assert build_package_lines(scan, config) == [
        ReportPackageLine("plan.bcast", PackageLineStatus.ACCEPTED, slots_total=5, slots_mine=3)
    ]
    assert build_skipped_lines(scan, selection, config) == [
        "- 15-03-2027 19:00 uk — уже прошло",
        "- 16-03-2027 12:30 ru — до старта меньше 60 минут",
        "- 17-03-2027 19:00 hu — нет канала для языка hu",
    ]


def test_write_report_uses_stamped_name(planer_paths: PlanerPaths) -> None:
    path: Path = write_report(planer_paths, "текст\n", datetime(2027, 3, 16, 12, 0, 5))
    assert path == planer_paths.logs_dir / "16-03-2027_120005_report.md"
    assert path.read_text(encoding="utf-8") == "текст\n"
