from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
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
    RunTotals,
    SkipKind,
    SkippedLine,
    build_package_lines,
    build_platform_note_lines,
    build_run_warning_lines,
    build_skipped_lines,
    build_totals,
    build_undated_warning_lines,
    build_warning_lines,
    error_texts,
    render_report,
    skip_text,
    write_report,
)
from app.package.bcast import BcastScan, scan_bcast
from app.paths import PlanerPaths
from app.pipeline.plan import WARNING_STEP_THUMBNAIL, Decision, OutcomeError, OutcomeWarning, PlannedBroadcast
from app.pipeline.selection import Selection, build_planned
from app.platforms.base import CreatedBroadcast, PlatformNotice, PlatformNoticeKind, StreamInfo, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import build_config, build_slot
from app.tests.conftest import build_planned as build_planned_object
from app.ui import messages_ru as msg
from app.version import APP_VERSION

STREAM_URL: str = "rtmp://a.rtmp.youtube.com/live2"

# Пример из ТЗ §5.6: итог и ошибки сверху, справка ниже, пустых разделов нет.
TZ_SAMPLE_REPORT: str = f"""# Planer {APP_VERSION} — отчёт 13-09-2026 12:00

Итог: опубликовано 2, исправлено 1, уже стояло 1, не публиковали 3, ошибок 1. Файл ключей: keystreams\\keys.txt

## Ключ не дошёл до стримера
- 18-09-2026 19:00 uk -> Канал UA — форма не подтвердила запись ответа (notConfirmed); эфир на канале стоит — передайте ключ стримеру из keys.txt вручную

## Ошибки
- 19-09-2026 19:00 uk -> Канал UA — YouTube: {msg.YOUTUBE_REASON_TEXT['liveStreamingNotEnabled']} (liveStreamingNotEnabled)

## Создано (2)
- 16-09-2026 19:00 uk -> Канал UA — эфир создан, ключ передан в форму
- 18-09-2026 19:00 uk -> Канал UA — эфир создан, ключ в форму НЕ передан — форма не подтвердила запись ответа (notConfirmed); повторно планер его не отправит, передайте ключ стримеру из keys.txt вручную

## Исправлено (1)
- 17-09-2026 19:00 uk -> Канал UA — на YouTube отличалось: описание; исправлено, ключ и ссылка прежние, ключ передан в форму

## Уже запланировано, совпадает (1)
- 16-09-2026 21:00 ru -> Канал RU — https://www.youtube.com/watch?v=def456

## Пропущено
- 14-09-2026 19:00 uk — уже прошло
- 15-09-2026 19:00 en — нет канала для языка en
- 13-09-2026 12:30 uk — до старта меньше 60 минут

## Пакеты
- plan_14-09-2026_25-09-2026_gen13-09-2026-1015.bcast — принят, слотов 24, из них под мои языки 9
- plan_07-09-2026_11-09-2026_gen06-09-2026-1000.bcast — все слоты в прошлом, ничего из него не планируется
"""

EMPTY_REPORT: str = f"""# Planer {APP_VERSION} — отчёт 16-03-2027 12:00

Итог: опубликовано 0, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0.
"""

DRY_RUN_REPORT: str = f"""# Planer {APP_VERSION} — отчёт 16-03-2027 12:00 (dry-run)
Внимание: Площадка — заглушка

Итог: опубликуем 1, исправим 0, уже стояло 0, не публиковали 0, ошибок 0.

## Создано (1)
- 17-03-2027 19:00 uk -> Test UA — эфира нет, будет создан — не выполнено (dry-run)
"""

STATUS_REPORT: str = f"""# Planer {APP_VERSION} — отчёт 16-03-2027 12:00

Итог: уже стояло 1, ошибок 1. Файл ключей: keystreams\\keys.txt

## Ошибки
- Test RU — YouTube: {msg.YOUTUBE_REASON_TEXT['quotaExceeded']} (quotaExceeded)

## Запланировано на каналах (1)
- 17-03-2027 19:00 uk -> Test UA — https://www.youtube.com/watch?v=abc
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
            PairOutcome(OutcomeKind.CREATED, "Канал UA", "16-09-2026", "19:00", "uk", form=FormState.SENT),
            PairOutcome(OutcomeKind.CREATED, "Канал UA", "18-09-2026", "19:00", "uk", form=FormState.FAILED),
            PairOutcome(
                OutcomeKind.FIXED, "Канал UA", "17-09-2026", "19:00", "uk",
                changed_fields=("description",), form=FormState.SENT,
            ),
            PairOutcome(
                OutcomeKind.MATCHED,
                "Канал RU",
                "16-09-2026",
                "21:00",
                "ru",
                broadcast_url="https://www.youtube.com/watch?v=def456",
            ),
            PairOutcome(
                OutcomeKind.ERROR,
                "Канал UA",
                "19-09-2026",
                "19:00",
                "uk",
                error=OutcomeError("youtube", "liveStreamingNotEnabled", "на канале не включены трансляции"),
            ),
        ],
        skipped=[
            SkippedLine(SkipKind.PAST, "14-09-2026", "19:00", "uk"),
            SkippedLine(SkipKind.NO_CHANNEL, "15-09-2026", "19:00", "en"),
            SkippedLine(SkipKind.TOO_LATE, "13-09-2026", "12:30", "uk", minutes=60),
        ],
        keys_file_path="keystreams\\keys.txt",
    )
    assert render_report(report) == TZ_SAMPLE_REPORT


def test_empty_sections_are_not_printed() -> None:
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


def test_orphans_section_appears_only_when_present() -> None:
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        orphans=[OrphanLine("19-03-2027", "12:00", "uk", "Test UA", "https://www.youtube.com/watch?v=old")],
    )
    text: str = render_report(report)
    assert (
        "## Перенесён или отменён? (1)\n"
        "- 19-03-2027 12:00 uk -> Test UA — https://www.youtube.com/watch?v=old — эфир не удалён\n"
    ) in text
    assert "## Уже запланировано" not in text
    assert "Перенесён или отменён" not in render_report(RunReport(RunMode.FULL, "16-03-2027 12:00"))


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
    assert "-> Test UA — u1\n" in text
    assert "отличалось: название, описание; исправлено, ключ и ссылка прежние\n" in text
    assert "эфир создан, ключ передан в форму\n" in text
    assert "несколько эфиров на эту минуту без маркера планера" in text
    assert "у найденного эфира нет привязанного потока" in text
    assert "журнал" not in text and "повторная отправка" not in text and "создан заново" not in text
    assert text.splitlines()[2] == "Итог: опубликовано 1, исправлено 1, уже стояло 1, не публиковали 0, ошибок 2."
    assert "✅" not in text and "❌" not in text


def test_errors_and_warnings_come_before_reference_sections() -> None:
    """Отчёт открывают ради ошибок и предупреждений — они сразу после итога, справка ниже."""
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        packages=[ReportPackageLine("plan.bcast", PackageLineStatus.ACCEPTED, slots_total=1, slots_mine=1)],
        outcomes=[
            _slot_outcome(OutcomeKind.CREATED, form=FormState.SENT),
            _slot_outcome(OutcomeKind.ERROR, error=OutcomeError("youtube", "forbidden", "нельзя")),
        ],
        warnings=["предупреждение", msg.WARNING_LIVE_CHAT],
        mismatches=["расхождение"],
        skipped=[SkippedLine(SkipKind.PAST, "15-03-2027", "19:00", "uk")],
    )
    headers: list[str] = [line for line in render_report(report).splitlines() if line.startswith("## ")]
    assert headers == [
        "## Ошибки",
        "## Предупреждения",
        "## Расхождения с платформой",
        "## Создано (1)",
        "## Пропущено",
        "## Пакеты",
        msg.REPORT_SECTION_NOTES,
    ]
    failed: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        outcomes=[_slot_outcome(OutcomeKind.CREATED, form=FormState.FAILED, form_error="transportFailed: HTTP 503")],
    )
    failed_headers: list[str] = [line for line in render_report(failed).splitlines() if line.startswith("## ")]
    assert failed_headers == ["## Ключ не дошёл до стримера", "## Создано (1)"]


def test_not_delivered_section_only_when_form_failed() -> None:
    """Ключ, не дошедший до стримера, — сразу под итогом; счётчики и раздел «Создано» прежние."""
    sent: RunReport = RunReport(RunMode.FULL, "16-03-2027 12:00", outcomes=[_slot_outcome(OutcomeKind.CREATED, form=FormState.SENT)])
    assert "Ключ не дошёл до стримера" not in render_report(sent)
    failed: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        outcomes=[
            _slot_outcome(OutcomeKind.CREATED, form=FormState.SENT),
            _slot_outcome(OutcomeKind.STREAM_ATTACHED, date="18-03-2027", form=FormState.FAILED, form_error="missingOption: 18.03.2027"),
        ],
    )
    lines: list[str] = render_report(failed).splitlines()
    assert lines[2] == "Итог: опубликовано 2, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0."
    assert lines[4] == "## Ключ не дошёл до стримера"
    assert lines[5].startswith("- 18-03-2027 19:00 uk -> Test UA — в форме нет нужного варианта ответа (18.03.2027)")
    assert "## Создано (2)" in lines
    assert build_totals(failed).errors == 0


def test_platform_notes_are_the_last_section_of_the_report() -> None:
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        warnings=["обложка не поставлена", msg.WARNING_KEPT_KEY, msg.WARNING_LIVE_CHAT],
    )
    assert report.run_warnings == ["обложка не поставлена"]
    assert report.notes == [msg.WARNING_KEPT_KEY, msg.WARNING_LIVE_CHAT]
    text: str = render_report(report)
    assert text.index("## Предупреждения") < text.index(msg.REPORT_SECTION_NOTES)
    assert text.rstrip("\n").endswith(f"- {msg.WARNING_LIVE_CHAT}")
    assert text.count(msg.WARNING_KEPT_KEY) == 1


def test_totals_are_counted_once_for_report_and_console() -> None:
    report: RunReport = RunReport(
        RunMode.FULL,
        "16-03-2027 12:00",
        packages=[
            ReportPackageLine("a.bcast", PackageLineStatus.ACCEPTED, slots_total=4, slots_mine=1),
            ReportPackageLine("b.bcast", PackageLineStatus.DAMAGED, detail="не ZIP-архив"),
        ],
        outcomes=[
            _slot_outcome(OutcomeKind.CREATED, form=FormState.SENT),
            _slot_outcome(OutcomeKind.STREAM_ATTACHED, form=FormState.FAILED),
            _slot_outcome(OutcomeKind.MATCHED),
            _slot_outcome(OutcomeKind.NO_STREAM),
        ],
    )
    assert build_totals(report) == RunTotals(
        packages=2,
        packages_unreadable=1,
        slots_total=4,
        slots_mine=1,
        created=2,
        form_sent=1,
        fixed=0,
        matched=1,
        orphans=0,
        skipped=0,
        errors=1,
    )


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


def test_undated_notices_are_deduplicated_by_channel_and_title() -> None:
    """Канал за запуск читается не раз: одинаковое замечание — одна строка предупреждения."""
    notice: PlatformNotice = PlatformNotice(PlatformNoticeKind.UNDATED_BROADCAST, "Test UA", "Брифинг")
    other: PlatformNotice = PlatformNotice(PlatformNoticeKind.UNDATED_BROADCAST, "Test RU", "Брифинг")
    lines: list[str] = build_undated_warning_lines([notice, other, notice])
    assert lines == [
        msg.WARNING_UNDATED_BROADCAST.format(account_name="Test UA", title="Брифинг"),
        msg.WARNING_UNDATED_BROADCAST.format(account_name="Test RU", title="Брифинг"),
    ]
    assert build_warning_lines([], (), [notice]) == lines[:1]


def test_kept_key_warning_is_written_once_per_run() -> None:
    kept: list[PlannedBroadcast] = [_with_found_key(17, Decision.MATCH), _with_found_key(18, Decision.UPDATE)]
    created: PlannedBroadcast = _with_new_key(19, Decision.CREATE)
    assert build_warning_lines([*kept, created]).count(msg.WARNING_KEPT_KEY) == 1
    assert msg.WARNING_KEPT_KEY not in build_warning_lines([created])
    attached: PlannedBroadcast = _with_new_key(20, Decision.MATCH)   # поток привязан: ключ новый
    assert msg.WARNING_KEPT_KEY not in build_warning_lines([attached])
    assert msg.WARNING_KEPT_KEY not in build_run_warning_lines([*kept, created])   # особенность, а не предупреждение
    assert build_platform_note_lines([*kept, created]) == [msg.WARNING_KEPT_KEY]


def test_package_and_skipped_lines_from_scan_and_selection(
    planer_paths: PlanerPaths,
    make_package: Callable[..., Path],
    make_slot: Callable[..., dict[str, Any]],
    make_config: Callable[..., PlanerConfig],
    now: datetime,
) -> None:
    make_package(
        planer_paths.bcast_dir,
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
    scan: BcastScan = scan_bcast(planer_paths, now)
    selection: Selection = build_planned(
        scan.slot_map, scan.slot_sources, config, FakePlatform().limits, now
    )
    assert build_package_lines(scan, config) == [
        ReportPackageLine("plan.bcast", PackageLineStatus.ACCEPTED, slots_total=5, slots_mine=3)
    ]
    skipped: list[SkippedLine] = build_skipped_lines(scan, selection, config)
    assert skipped == [
        SkippedLine(SkipKind.PAST, "15-03-2027", "19:00", "uk", title="Эфир 15-03-2027_1900_uk"),
        SkippedLine(SkipKind.TOO_LATE, "16-03-2027", "12:30", "ru", minutes=60, title="Эфир 16-03-2027_1230_ru"),
        SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "hu", title="Эфир 17-03-2027_1900_hu"),
    ]
    assert [skip_text(line) for line in skipped] == [
        "15-03-2027 19:00 uk — уже прошло",
        "16-03-2027 12:30 ru — до старта меньше 60 минут",
        "17-03-2027 19:00 hu — нет канала для языка hu",
    ]


def test_write_report_uses_stamped_name(planer_paths: PlanerPaths) -> None:
    path: Path = write_report(planer_paths, "текст\n", datetime(2027, 3, 16, 12, 0, 5))
    assert path == planer_paths.logs_dir / "16-03-2027_120005_report.md"
    assert path.read_text(encoding="utf-8") == "текст\n"


def _thumbnail_item(code: str, message: str) -> PlannedBroadcast:
    item: PlannedBroadcast = build_planned_object(
        build_slot(datetime(2027, 3, 17, 19, 0, tzinfo=timezone(timedelta(hours=2))), "uk"),
        build_config().channels[0],
    )
    item.warn(OutcomeWarning(WARNING_STEP_THUMBNAIL, code, message))
    return item


def test_thumbnail_upload_limit_is_explained_without_verified_channel_hint() -> None:
    [line] = build_run_warning_lines([_thumbnail_item("uploadRateLimitExceeded", "HTTP 429: limit")])
    assert line == (
        "17-03-2027 19:00 uk -> yt_ua: " + msg.WARNING_STEP_TEXT["thumbnail"] + " — "
        + msg.THUMBNAIL_REASON_TEXT["uploadRateLimitExceeded"]
    )
    assert "подтверждённ" not in line and "подтвердите" not in line


def test_unknown_thumbnail_reason_keeps_code_and_google_message() -> None:
    [line] = build_run_warning_lines([_thumbnail_item("somethingNew", "HTTP 400: странное")])
    assert line.endswith(msg.WARNING_STEP_TEXT["thumbnail"] + " — somethingNew (HTTP 400: странное)")


def test_youtube_error_with_known_reason_is_explained_with_code() -> None:
    outcome: PairOutcome = PairOutcome(
        OutcomeKind.ERROR, "Канал UA", "17-03-2027", "19:00", "uk",
        error=OutcomeError("youtube", "quotaExceeded", "HTTP 403: quota"),
    )
    [line] = error_texts(RunReport(mode=RunMode.FULL, generated_at_text="x", outcomes=[outcome]))
    assert line == (
        "17-03-2027 19:00 uk -> Канал UA — YouTube: " + msg.YOUTUBE_REASON_TEXT["quotaExceeded"] + " (quotaExceeded)"
    )


def test_youtube_error_with_unknown_reason_keeps_old_format() -> None:
    outcome: PairOutcome = PairOutcome(
        OutcomeKind.ERROR, "Канал UA", "17-03-2027", "19:00", "uk",
        error=OutcomeError("youtube", "somethingNew", "HTTP 400: странное"),
    )
    [line] = error_texts(RunReport(mode=RunMode.FULL, generated_at_text="x", outcomes=[outcome]))
    assert line == "17-03-2027 19:00 uk -> Канал UA — YouTube: somethingNew (HTTP 400: странное)"
