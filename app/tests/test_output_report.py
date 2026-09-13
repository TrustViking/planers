from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.report import (
    FormState,
    OrphanLine,
    OutcomeKind,
    PackageLineStatus,
    PairOutcome,
    ReportPackageLine,
    RunMode,
    RunReport,
    build_package_lines,
    build_skipped_lines,
    build_warning_lines,
    render_report,
    write_report,
)
from app.package.promo import PromoScan, scan_promo
from app.paths import PlanerPaths
from app.pipeline.plan import Decision, OutcomeError, PlannedBroadcast
from app.pipeline.selection import Selection, build_planned
from app.platforms.base import CreatedBroadcast, StreamInfo, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import build_config, build_slot
from app.tests.conftest import build_planned as build_planned_object
from app.ui import messages_ru as msg

STREAM_URL: str = "rtmp://a.rtmp.youtube.com/live2"

# Пример из ТЗ §5.6 с согласованными счётчиками (в ТЗ строки разделов даны выборочно).
TZ_SAMPLE_REPORT: str = """# Планер — отчёт 13-09-2026 12:00

## Пакеты
- plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast — принят, слотов 24, из них под мои языки 9
- plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast — все слоты в прошлом, ничего из него не планируется

## Создано (2)
- 16-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ✅
- 18-09-2026 19:00 uk → Іван UA — эфир создан, ключ получен, форма ❌ форма не подтвердила запись ответа (notConfirmed); повторно планер ключ не отправит — передайте его стримеру из keys.txt вручную

## Исправлено (1)
- 17-09-2026 19:00 uk → Іван UA — на YouTube было другое описание; обновлено. Ключ и ссылка прежние

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

EMPTY_REPORT: str = """# Планер — отчёт 16-03-2027 12:00

## Пакеты

## Создано (0)

## Исправлено (0)

## Уже запланировано, совпадает (0)

## Пропущено

## Ошибки

Итог: создано 0, исправлено 0, копий 0, пропущено 0, ошибок 0.
"""

DRY_RUN_REPORT: str = """# Планер — отчёт 16-03-2027 12:00 (dry-run)
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

STATUS_REPORT: str = """# Планер — отчёт 16-03-2027 12:00

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
    assert render_report(RunReport(RunMode.FULL, "16-03-2027 12:00")) == EMPTY_REPORT


def test_dry_run_marks_title_and_every_outcome() -> None:
    report: RunReport = RunReport(
        RunMode.DRY_RUN,
        "16-03-2027 12:00",
        outcomes=[_slot_outcome(OutcomeKind.CREATED)],
        notice="Площадка — заглушка",
    )
    assert render_report(report) == DRY_RUN_REPORT


def test_status_report_structure() -> None:
    report: RunReport = RunReport(
        RunMode.STATUS,
        "16-03-2027 12:00",
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
        orphans=[OrphanLine("19-03-2027", "12:00", "uk", "Test UA", "https://www.youtube.com/watch?v=old")],
    )
    text: str = render_report(report)
    assert (
        "## Уже запланировано, совпадает (0)\n\n"
        "## Перенесён или отменён? (1)\n"
        "- 19-03-2027 12:00 uk → Test UA — https://www.youtube.com/watch?v=old — эфир не удалён\n"
    ) in text


def test_matched_fixed_ambiguous_and_planer_error_texts() -> None:
    """Про журнал и повторную отправку отчёт больше ничего не говорит: планер этого не знает."""
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        outcomes=[
            _slot_outcome(OutcomeKind.MATCHED, broadcast_url="u1"),
            _slot_outcome(OutcomeKind.FIXED, changed_fields=("title", "description")),
            _slot_outcome(OutcomeKind.CREATED, form=FormState.SENT),
            _slot_outcome(OutcomeKind.AMBIGUOUS),
            _slot_outcome(OutcomeKind.ERROR, error=OutcomeError("planer", "noBoundStream")),
        ],
    )
    text: str = render_report(report)
    assert "→ Test UA — u1\n" in text
    assert "другое название и описание; обновлено. Ключ и ссылка прежние\n" in text
    assert "эфир создан, ключ получен, форма ✅\n" in text
    assert "несколько эфиров на эту минуту без маркера планера" in text
    assert "у найденного эфира нет привязанного потока" in text
    assert "журнал" not in text and "повторная отправка" not in text and "создан заново" not in text
    assert text.endswith("ошибок 2.\n")


def _object(day: int, decision: Decision) -> PlannedBroadcast:
    return build_planned_object(
        build_slot(datetime.fromisoformat(f"2027-03-{day:02d}T19:00:00+02:00"), "uk"),
        build_config().channels[0],
    )


def _with_found_key(day: int, decision: Decision) -> PlannedBroadcast:
    """Эфир найден сверкой: ключ с площадки, не новый."""
    item: PlannedBroadcast = _object(day, decision)
    item.found = UpcomingBroadcast(f"bc{day}", item.slot.start, item.slot.title, item.slot.description, f"s{day}")
    item.found_stream = StreamInfo(f"s{day}", item.slot_id, STREAM_URL, f"k{day:03d}-aaaa-aaaa-aaaa-aaaa")
    item.take_found_key()
    item.decision = decision
    return item


def _with_new_key(day: int, decision: Decision) -> PlannedBroadcast:
    """Эфир создан или к нему привязан поток: ключ получен в этом запуске."""
    item: PlannedBroadcast = _object(day, decision)
    item.take_new_key(
        CreatedBroadcast(f"bc{day}", f"https://www.youtube.com/watch?v=bc{day}", f"s{day}", STREAM_URL, f"k{day:03d}-bbbb-bbbb-bbbb-bbbb")
    )
    item.decision = decision
    return item


def test_kept_key_warning_is_written_once_per_run() -> None:
    kept: list[PlannedBroadcast] = [_with_found_key(17, Decision.MATCH), _with_found_key(18, Decision.UPDATE)]
    created: PlannedBroadcast = _with_new_key(19, Decision.CREATE)
    assert build_warning_lines([*kept, created]).count(msg.WARNING_KEPT_KEY) == 1
    assert msg.WARNING_KEPT_KEY not in build_warning_lines([created])
    attached: PlannedBroadcast = _with_new_key(20, Decision.MATCH)   # поток привязан: ключ новый
    assert msg.WARNING_KEPT_KEY not in build_warning_lines([attached])


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
    selection: Selection = build_planned(
        scan.slot_map, scan.slot_sources, config, FakePlatform().limits, now
    )
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
