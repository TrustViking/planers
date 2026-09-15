from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config.loader import PlanerConfig
from app.output.console import render_console
from app.output.report import (
    FieldChange,
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
OSVALD: dict[str, str] = {"account_name": "Osvald.X", "google_account": "trustviorel@gmail.com"}
OKTAVIAN: dict[str, str] = {"account_name": "Oktavian.X", "google_account": "oktavian.tibery@gmail.com"}
CHANNEL_ORDER: tuple[str, ...] = ("Osvald.X", "Oktavian.X")      # как в channels.json
FULL_KEYS: tuple[str, ...] = ("aaaa-bbbb-cccc-dddd-6jty", "aaaa-bbbb-cccc-dddd-3j1j", "aaaa-bbbb-cccc-dddd-9zzz")
UNDATED_WARNING: str = msg.WARNING_UNDATED_BROADCAST.format(account_name="Osvald.X", title="Брифинг в Конгрессе")
RESTORED_WARNING: str = (
    "не можем исправить: 19-03-2027 19:00 uk -> Oktavian.X — автостарт: нужно да, на площадке нет; "
    "через API это не исправляется"
)

# Макет задачи 5e: блоки сверху вниз, пустой блок не печатается, каналы — в порядке channels.json.
SAMPLE_CONSOLE: str = f"""Итог: опубликовано 2, исправлено 1, уже стояло 1, не публиковали 2, ошибок 0

======================= ВНИМАНИЕ =======================
  ключ не дошёл до стримера: 18-03-2027 20:00 ru -> Osvald.X — форма недоступна (HTTP 503)
  вернули к пакету: 18-03-2027 20:00 ru -> Osvald.X — видимость: было private, стало unlisted
  {RESTORED_WARNING}
  {UNDATED_WARNING}

=================== ОПУБЛИКОВАЛИ (2) ===================
  Osvald.X (trustviorel@gmail.com)
    17-03-2027  19:00  ru  Контроль влажности экономит до 40% энергии
  Oktavian.X (oktavian.tibery@gmail.com)
    17-03-2027  19:00  uk  Депортовані діти мають повернутися

==================== ИСПРАВИЛИ (1) =====================
  Osvald.X (trustviorel@gmail.com)
    18-03-2027  20:00  ru  Второй эфир — обновлено: описание

================== КЛЮЧИ СТРИМЕРУ (3) ==================
  Osvald.X (trustviorel@gmail.com)
    17-03-2027  19:00  ru  ****-6jty  передан в форму
    18-03-2027  20:00  ru  ****-9zzz  НЕ передан — форма недоступна (HTTP 503)
  Oktavian.X (oktavian.tibery@gmail.com)
    17-03-2027  19:00  uk  ****-3j1j  передан в форму

==================== УЖЕ СТОЯЛО (1) ====================
  Oktavian.X (oktavian.tibery@gmail.com)
    19-03-2027  19:00  uk  Третій ефір

================== НЕ ПУБЛИКОВАЛИ (2) ==================
  нет канала для языка en
    17-03-2027  19:00  en  Russia's 20-Year Hybrid War
    18-03-2027  20:00  en  The Invisible Side of Air

  ключи   keystreams\\keys.txt
  отчёт   logs\\15-09-2026_195649_report.md
  лог     logs\\15-09-2026_195649_planer.log"""


def _outcomes() -> list[PairOutcome]:
    """Порядок исходов — как их отдаёт runner (по слоту, потом по имени канала), а не как в channels.json."""
    return [
        PairOutcome(
            OutcomeKind.CREATED, **OKTAVIAN, date="17-03-2027", time="19:00", language="uk",
            form=FormState.SENT, title="Депортовані діти мають повернутися", stream_key=FULL_KEYS[1],
        ),
        PairOutcome(
            OutcomeKind.CREATED, **OSVALD, date="17-03-2027", time="19:00", language="ru",
            form=FormState.SENT, title="Контроль влажности экономит до 40% энергии", stream_key=FULL_KEYS[0],
        ),
        PairOutcome(
            OutcomeKind.FIXED, **OSVALD, date="18-03-2027", time="20:00", language="ru",
            form=FormState.FAILED, form_error="transportFailed: HTTP 503", title="Второй эфир", stream_key=FULL_KEYS[2],
            changed_fields=("description", "privacy"),
            field_changes=(FieldChange("description", "-", "-"), FieldChange("privacy", "private", "unlisted")),
        ),
        PairOutcome(
            OutcomeKind.MATCHED, **OKTAVIAN, date="19-03-2027", time="19:00", language="uk",
            title="Третій ефір", stream_key="aaaa-bbbb-cccc-dddd-1111",
        ),
    ]


def _sample_report(**overrides: Any) -> RunReport:
    values: dict[str, Any] = dict(
        mode=RunMode.FULL,
        generated_at_text="15-09-2026 19:56",
        packages=[ReportPackageLine("plan.bcast", PackageLineStatus.ACCEPTED, slots_total=6, slots_mine=4)],
        outcomes=_outcomes(),
        skipped=[
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "en", title="Russia's 20-Year Hybrid War"),
            SkippedLine(SkipKind.NO_CHANNEL, "18-03-2027", "20:00", "en", title="The Invisible Side of Air"),
        ],
        warnings=[RESTORED_WARNING, UNDATED_WARNING, msg.WARNING_LIVE_CHAT, msg.WARNING_KEPT_KEY],
        keys_file_path="keystreams\\keys.txt",
    )
    values.update(overrides)
    return RunReport(**values)


def _render(report: RunReport) -> str:
    return render_console(
        report,
        root=ROOT,
        report_path=ROOT / "logs" / "15-09-2026_195649_report.md",
        log_path=ROOT / "logs" / "15-09-2026_195649_planer.log",
        channel_order=CHANNEL_ORDER,
    )


def _block_titles(text: str) -> list[str]:
    return [line.strip(msg.CONSOLE_RULE_CHAR + " ") for line in text.splitlines() if line.startswith(msg.CONSOLE_RULE_CHAR)]


def test_full_run_matches_the_layout() -> None:
    assert _render(_sample_report()).replace("/", "\\") == SAMPLE_CONSOLE


def test_console_has_no_title_it_is_printed_by_main_at_start() -> None:
    """Шапку печатает main при старте: в итоговом тексте её нет, иначе в прогоне было бы два заголовка."""
    for mode in RunMode:
        text: str = _render(_sample_report(mode=mode))
        assert text.splitlines()[0].startswith("Итог: ")
        assert "Планер " not in text


def test_blocks_keep_their_order_and_rules_their_width() -> None:
    text: str = _render(_sample_report())
    assert _block_titles(text) == [
        "ВНИМАНИЕ", "ОПУБЛИКОВАЛИ (2)", "ИСПРАВИЛИ (1)", "КЛЮЧИ СТРИМЕРУ (3)", "УЖЕ СТОЯЛО (1)", "НЕ ПУБЛИКОВАЛИ (2)",
    ]
    rules: list[str] = [line for line in text.splitlines() if line.startswith(msg.CONSOLE_RULE_CHAR)]
    assert {len(line) for line in rules} == {msg.CONSOLE_RULE_WIDTH}


def test_empty_blocks_are_not_printed_at_all() -> None:
    """Нули видны в «Итоге»; пустого раздела нет."""
    text: str = _render(_sample_report(outcomes=[], skipped=[], warnings=[]))
    lines: list[str] = text.splitlines()
    assert lines[0] == "Итог: опубликовано 0, исправлено 0, уже стояло 0, не публиковали 0, ошибок 0"
    assert _block_titles(text) == []


def test_broadcasts_are_grouped_by_channel_in_channels_json_order() -> None:
    """Шапка канала с почтой — один раз на группу; внутри канала — по дате и времени."""
    text: str = _render(_sample_report())
    keys_block: list[str] = text.split("КЛЮЧИ СТРИМЕРУ (3)")[1].split("\n\n")[0].splitlines()[1:]
    assert keys_block[0] == "  Osvald.X (trustviorel@gmail.com)"
    assert keys_block[1].startswith("    17-03-2027  19:00") and keys_block[2].startswith("    18-03-2027  20:00")
    assert keys_block[3] == "  Oktavian.X (oktavian.tibery@gmail.com)"
    assert text.count("  Osvald.X (trustviorel@gmail.com)") == 3        # по разу в каждом блоке, где канал есть
    assert "-> Osvald.X" not in text.split("ОПУБЛИКОВАЛИ (2)")[1]         # в строках эфиров канала нет


def test_stream_key_is_masked_and_the_full_key_appears_nowhere() -> None:
    text: str = _render(_sample_report())
    assert "****-6jty" in text and "****-9zzz" in text
    assert all(key not in line for key in FULL_KEYS for line in text.splitlines())


def test_keys_file_path_is_printed_once_in_the_footer() -> None:
    """Путь к keys.txt — только строкой подвала, блок КЛЮЧИ СТРИМЕРУ его не повторяет."""
    text: str = _render(_sample_report())
    assert text.count("keystreams") == 1
    assert text.splitlines()[-3].startswith(f"  {msg.CONSOLE_LABEL_KEYS}")


def test_settings_only_fix_has_no_tail_in_fixed_and_is_named_in_attention() -> None:
    """Изменилась только видимость: в ИСПРАВИЛИ — строка без «обновлено», во ВНИМАНИЕ — было/стало."""
    report: RunReport = _sample_report(
        outcomes=[
            PairOutcome(
                OutcomeKind.FIXED, **OSVALD, date="18-03-2027", time="20:00", language="ru",
                form=FormState.SENT, title="Второй эфир", stream_key=FULL_KEYS[2],
                changed_fields=("privacy",), field_changes=(FieldChange("privacy", "private", "unlisted"),),
            ),
        ],
        skipped=[],
        warnings=[],
    )
    text: str = _render(report)
    lines: list[str] = text.splitlines()
    fixed: list[str] = text.split("ИСПРАВИЛИ (1)")[1].split("\n\n")[0].splitlines()[1:]
    assert fixed == ["  Osvald.X (trustviorel@gmail.com)", "    18-03-2027  20:00  ru  Второй эфир"]
    assert lines[0] == "Итог: опубликовано 0, исправлено 1, уже стояло 0, не публиковали 0, ошибок 0"
    assert "  вернули к пакету: 18-03-2027 20:00 ru -> Osvald.X — видимость: было private, стало unlisted" in lines
    assert text.count("видимость") == 1


def test_console_has_no_markdown_and_no_icons() -> None:
    text: str = _render(_sample_report())
    assert "#" not in text and "\n- " not in text
    assert all(icon not in text for icon in ("✅", "❌", "⚠", "→"))


def test_attention_collects_errors_forms_packages_restored_and_warnings() -> None:
    report: RunReport = _sample_report(
        packages=[ReportPackageLine("broken.bcast", PackageLineStatus.DAMAGED, detail="не ZIP-архив")],
        outcomes=[
            *_outcomes(),
            PairOutcome(
                OutcomeKind.ERROR, **OSVALD, date="20-03-2027", time="20:00", language="ru",
                error=OutcomeError("youtube", "liveStreamingNotEnabled", "на канале не включены трансляции"),
            ),
            PairOutcome(OutcomeKind.AMBIGUOUS, **OSVALD, date="21-03-2027", time="20:00", language="ru"),
        ],
    )
    text: str = _render(report)
    attention: list[str] = text.split("\n\n")[1].splitlines()
    assert msg.CONSOLE_BLOCK_ATTENTION in attention[0]
    assert (
        "  ошибка: 20-03-2027 20:00 ru -> Osvald.X — YouTube: liveStreamingNotEnabled (на канале не включены трансляции)"
    ) in attention
    assert "  пакет: broken.bcast — пакет повреждён: не ZIP-архив; файл не тронут" in attention
    assert f"  {UNDATED_WARNING}" in attention
    # AMBIGUOUS приходит предупреждением со ссылками — строкой ошибки не дублируется
    assert not any("несколько эфиров" in line for line in attention)
    # постоянные особенности площадки — только в отчёте
    assert "живой чат" not in text and "повторно не отправляется" not in text


def test_dry_run_speaks_of_intent_and_has_no_keys_block() -> None:
    fixed: PairOutcome = _outcomes()[2]
    report: RunReport = _sample_report(
        mode=RunMode.DRY_RUN,
        outcomes=[
            PairOutcome(OutcomeKind.CREATED, **OSVALD, date="17-03-2027", time="19:00", language="ru", title="Эфир"),
            PairOutcome(
                OutcomeKind.FIXED, **OSVALD, date="18-03-2027", time="20:00", language="ru", title="Второй эфир",
                changed_fields=fixed.changed_fields, field_changes=fixed.field_changes,
            ),
        ],
        warnings=[],
        keys_file_path=None,
    )
    text: str = _render(report)
    lines: list[str] = text.splitlines()
    assert lines[0] == "Итог: опубликуем 1, исправим 1, уже стояло 0, не публиковали 2, ошибок 0"
    assert _block_titles(text) == ["ВНИМАНИЕ", "ОПУБЛИКУЕМ (1)", "ИСПРАВИМ (1)", "НЕ ПУБЛИКОВАЛИ (2)"]
    assert "    18-03-2027  20:00  ru  Второй эфир — будет обновлено: описание" in lines
    assert "  вернём к пакету: 18-03-2027 20:00 ru -> Osvald.X — видимость: сейчас private, будет unlisted" in lines
    assert msg.CONSOLE_BLOCK_KEYS not in text and msg.CONSOLE_LABEL_KEYS + " " not in text


def test_status_prints_total_attention_and_matched_only() -> None:
    report: RunReport = RunReport(
        mode=RunMode.STATUS,
        generated_at_text="15-09-2026 19:56",
        outcomes=[
            PairOutcome(
                OutcomeKind.MATCHED, **OSVALD, date="17-03-2027", time="19:00", language="ru",
                title="Эфир", stream_key=FULL_KEYS[0], broadcast_url="u1",
            ),
            PairOutcome(OutcomeKind.ERROR, "Test RU", error=OutcomeError("youtube", "quotaExceeded", "квота исчерпана")),
        ],
        keys_file_path="keystreams\\keys.txt",
    )
    text: str = _render(report)
    lines: list[str] = text.splitlines()
    assert lines[0] == "Итог: уже стояло 1, ошибок 1"
    assert _block_titles(text) == ["ВНИМАНИЕ", "УЖЕ СТОЯЛО (1)"]
    assert "  ошибка: Test RU — YouTube: quotaExceeded (квота исчерпана)" in lines
    assert "    17-03-2027  19:00  ru  Эфир" in lines
    assert FULL_KEYS[0] not in text


def test_skipped_are_grouped_by_reason_not_by_channel() -> None:
    report: RunReport = _sample_report(
        outcomes=[],
        warnings=[],
        skipped=[
            SkippedLine(SkipKind.PAST, "14-03-2027", "19:00", "ru", title="Прошлый"),
            SkippedLine(SkipKind.TOO_LATE, "16-03-2027", "12:30", "ru", minutes=60, title="Скоро"),
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "hu", title="Magyar"),
            SkippedLine(SkipKind.NO_CHANNEL, "17-03-2027", "19:00", "en", title="English"),
        ],
    )
    block: list[str] = _render(report).split("НЕ ПУБЛИКОВАЛИ (4)")[1].split("\n\n")[0].splitlines()[1:]
    assert block == [
        "  уже прошло",
        "    14-03-2027  19:00  ru  Прошлый",
        "  до старта меньше 60 минут",
        "    16-03-2027  12:30  ru  Скоро",
        "  нет канала для языка en",
        "    17-03-2027  19:00  en  English",
        "  нет канала для языка hu",
        "    17-03-2027  19:00  hu  Magyar",
    ]


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
    """Production-путь: запуск через runner, «Итог» консоли — те же числа, что «Итог» записанного отчёта."""
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
    assert console.splitlines()[0] == msg.CONSOLE_TOTAL.format(
        created=totals.created, fixed=totals.fixed, matched=totals.matched, skipped=totals.skipped, errors=totals.errors
    )
    assert (totals.created, totals.skipped, totals.errors) == (1, 1, 1)
    assert "    17-03-2027  19:00  uk  Эфир 17-03-2027_1900_uk" in console.splitlines()
    assert f"  отчёт   {Path('logs') / outcome.report_path.name}" in console
