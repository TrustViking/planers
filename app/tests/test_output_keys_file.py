from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config.loader import ChannelConfig, Platform, Privacy
from app.output.keys_file import (
    form_status_text,
    key_row_from_marked,
    key_row_from_planned,
    render_keys_file,
    write_keys_file,
)
from app.paths import PlanerPaths
from app.pipeline.plan import AdmissionKind, AdmissionReason, PlannedBroadcast
from app.pipeline.reconciler import MarkedBroadcast, MarkerParts
from app.records.slot_record import RecordResults, SlotRecord, SlotStage
from app.platforms.base import CreatedBroadcast, StreamInfo, UpcomingBroadcast
from app.tests.conftest import build_planned, build_slot
from app.ui import messages_ru as msg

KYIV: timezone = timezone(timedelta(hours=2))
STREAM_URL: str = "rtmp://a.rtmp.youtube.com/live2"

# Образец ТЗ §5.5: блок на стрим, ключ — первой строкой блока. Статус формы говорит только
# об этом запуске — отправлен сейчас, НЕ отправлен новый ключ, или эфир уже стоял с прежним ключом.
TZ_SAMPLE_KEYS: str = """# Ключи трансляций. Сгенерировано планером 13-09-2026 12:00.
# Файл перезаписывается на каждом запуске — не править.
# Строка «форма»:
#   «отправлен в форму» — форма подтвердила ключ в этом запуске;
#   «передан в форму» — форма подтвердила этот ключ раньше (память планера);
#   «передан до появления памяти планера» — эфир уже стоял с меткой планера, когда память создавалась;
#   «НЕ отправлен» — ключ должен был уйти и не ушёл: передайте его стримеру вручную;
#   «НЕ отправлен: не допущено» — форма этот эфир не принимает (нет даты или варианта) или канал не подтверждён: эфир стоит, ключ стримеру не передан — передайте вручную;
#   «нет подтверждения в памяти планера» — планер не знает, получил ли стример этот ключ.

16-09-2026 19:00  uk  Канал UA @КаналUA
  ключ   xxxx-xxxx-xxxx-xxxx-xxxx
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=abc123
  форма  отправлен в форму 13-09-2026 12:00

16-09-2026 21:00  ru  Канал RU @КаналRU
  ключ   yyyy-yyyy-yyyy-yyyy-yyyy
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=def456
  форма  НЕ отправлен — форма недоступна (HTTP 503); передайте стримеру вручную

17-09-2026 19:00  uk  Канал UA @КаналUA
  ключ   zzzz-zzzz-zzzz-zzzz-zzzz
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=ghi789
  форма  передан в форму 12-09-2026 20:00
"""


def _channel(account_name: str, language: str) -> ChannelConfig:
    return ChannelConfig(
        platform=Platform.YOUTUBE,
        account_name=account_name,
        handle="@" + account_name.replace(" ", ""),
        google_account="owner@gmail.com",
        languages=(language,),
        privacy=Privacy.PUBLIC,
    )


UA: ChannelConfig = _channel("Канал UA", "uk")
RU: ChannelConfig = _channel("Канал RU", "ru")


def _start(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=KYIV)


def _new_key(day: int, hour: int, language: str, channel: ChannelConfig, broadcast_id: str, key: str) -> PlannedBroadcast:
    """Эфир создан в этом запуске: ключ пришёл из ответа площадки."""
    item: PlannedBroadcast = build_planned(build_slot(_start(day, hour), language), channel)
    item.take_new_key(
        CreatedBroadcast(
            broadcast_id=broadcast_id,
            broadcast_url=f"https://www.youtube.com/watch?v={broadcast_id}",
            stream_id="s1",
            stream_url=STREAM_URL,
            stream_key=key,
        )
    )
    return item


def _confirmed(item: PlannedBroadcast, *, is_bootstrap: bool = False, at: str = "12-09-2026 20:00") -> PlannedBroadcast:
    """Память хранит подтверждение текущего ключа объекта."""
    item.record = SlotRecord(
        slot_id=item.slot_id,
        youtube_channel_id="UC1",
        slot_start_utc=item.slot.start.isoformat(),
        stage=SlotStage.KEY_CONFIRMED,
        updated_at=at,
        results=RecordResults(confirmed_stream_key=item.stream_key, confirmed_at=at, is_bootstrap=is_bootstrap),
    )
    return item


def _found_key(day: int, hour: int, language: str, channel: ChannelConfig, broadcast_id: str, key: str) -> PlannedBroadcast:
    """Эфир найден сверкой: ключ — тот, что сейчас на площадке."""
    item: PlannedBroadcast = build_planned(build_slot(_start(day, hour), language), channel)
    item.found = UpcomingBroadcast(broadcast_id, _start(day, hour), "Эфир", "", "s1")
    item.found_stream = StreamInfo("s1", item.slot_id, STREAM_URL, key)
    item.take_found_key()
    return item


def test_render_matches_tz_sample() -> None:
    sent: PlannedBroadcast = _new_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    sent.is_form_sent = True
    sent.form_sent_at = datetime(2026, 9, 13, 12, 0)
    failed: PlannedBroadcast = _new_key(16, 21, "ru", RU, "def456", "yyyy-yyyy-yyyy-yyyy-yyyy")
    failed.last_error = "transportFailed: HTTP 503"
    failed.should_send_key = True
    kept: PlannedBroadcast = _confirmed(_found_key(17, 19, "uk", UA, "ghi789", "zzzz-zzzz-zzzz-zzzz-zzzz"))
    rows = [key_row_from_planned(kept), key_row_from_planned(failed), key_row_from_planned(sent)]
    assert render_keys_file(rows, "13-09-2026 12:00") == TZ_SAMPLE_KEYS


def test_row_takes_everything_from_the_object() -> None:
    """Дата, время, язык и аккаунт — из слота и канала; ключ и ссылка — с площадки."""
    item: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    row = key_row_from_planned(item)
    assert (row.date, row.time, row.language, row.account_name) == ("16-09-2026", "19:00", "uk", "Канал UA")
    assert (row.stream_key, row.broadcast_url) == ("xxxx-xxxx-xxxx-xxxx-xxxx", "https://www.youtube.com/watch?v=abc123")


def test_object_without_key_shows_dashes() -> None:
    item: PlannedBroadcast = build_planned(build_slot(_start(16, 19), "uk"), UA)
    row = key_row_from_planned(item)
    assert (row.stream_key, row.stream_url, row.broadcast_url) == ("-", "-", "-")


def test_form_status_texts_speak_about_this_run_then_the_memory() -> None:
    new: PlannedBroadcast = _new_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    new.should_send_key = True
    new.last_error = "notConfirmed: HTTP 200"
    assert form_status_text(new) == "НЕ отправлен — форма не подтвердила запись ответа (HTTP 200); передайте стримеру вручную"
    new.is_form_sent = True
    new.form_sent_at = datetime(2026, 9, 13, 12, 0)
    assert form_status_text(new) == "отправлен в форму 13-09-2026 12:00"
    found: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    assert form_status_text(found) == "нет подтверждения в памяти планера"
    assert form_status_text(_confirmed(found)) == "передан в форму 12-09-2026 20:00"
    assert form_status_text(_confirmed(found, is_bootstrap=True)) == "передан до появления памяти планера"
    found.stream_key = "wwww-wwww-wwww-wwww-wwww"         # подтверждение было про другой ключ
    assert form_status_text(found) == "нет подтверждения в памяти планера"


def test_status_row_reads_the_confirmation_from_memory() -> None:
    marked: MarkedBroadcast = MarkedBroadcast(
        channel=UA,
        broadcast=UpcomingBroadcast("abc123", _start(16, 19), "Эфир", "", "s1"),
        stream=StreamInfo("s1", "16-09-2026_1900_uk", STREAM_URL, "xxxx-xxxx-xxxx-xxxx-xxxx"),
        parts=MarkerParts(date="16-09-2026", time="19:00", language="uk"),
    )
    row = key_row_from_marked(marked)
    assert (row.form_status_text, row.stream_key) == ("нет подтверждения в памяти планера", "xxxx-xxxx-xxxx-xxxx-xxxx")
    confirmed = RecordResults(confirmed_stream_key="xxxx-xxxx-xxxx-xxxx-xxxx", confirmed_at="12-09-2026 20:00")
    assert key_row_from_marked(marked, confirmed).form_status_text == "передан в форму 12-09-2026 20:00"


def test_empty_file_has_only_comment_lines(planer_paths: PlanerPaths) -> None:
    path: Path = write_keys_file(planer_paths, render_keys_file([], "16-03-2027 12:00"))
    assert path == planer_paths.keys_file
    lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(msg.KEYS_FILE_HEADER) and all(line.startswith("# ") for line in lines)


def test_key_is_the_first_line_of_each_block() -> None:
    """Ключ — ради него файл и открывают — стоит первым в блоке, а не в конце длинной строки."""
    item: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    lines: list[str] = render_keys_file([key_row_from_planned(item)], "13-09-2026 12:00").splitlines()
    header: int = len(msg.KEYS_FILE_HEADER)
    assert lines[header] == ""
    assert lines[header + 1] == "16-09-2026 19:00  uk  Канал UA @КаналUA"
    assert lines[header + 2] == "  ключ   xxxx-xxxx-xxxx-xxxx-xxxx"
    assert all(len(line) <= 80 for line in lines[header + 1 :])


def _not_admitted(day: int, hour: int, language: str, channel: ChannelConfig, broadcast_id: str, key: str) -> PlannedBroadcast:
    """Эфир стоит на канале, но форма его не принимает: нет варианта даты."""
    item: PlannedBroadcast = _found_key(day, hour, language, channel, broadcast_id, key)
    item.admission_reasons = (
        AdmissionReason(AdmissionKind.FORM_FIELD, "missingOption", "date", "Время стрима ( Stream time ): 18.09.2026"),
    )
    return item


def test_not_admitted_key_says_why_it_was_not_sent() -> None:
    item: PlannedBroadcast = _not_admitted(18, 20, "en", UA, "qJjIZCbP89s", "wwww-wwww-wwww-wwww-wwww")
    assert form_status_text(item) == (
        "НЕ отправлен: не допущено — в форме нет варианта «Время стрима ( Stream time ): 18.09.2026» "
        "— нужен владельцу формы"
    )
    row = key_row_from_planned(item)
    assert (row.stream_key, row.broadcast_url) == ("wwww-wwww-wwww-wwww-wwww", "https://www.youtube.com/watch?v=qJjIZCbP89s")


def test_header_quotes_the_real_form_lines() -> None:
    """Тексты в кавычках шапки — ровно начала строк «форма», которые пишет файл, во всех шести состояниях."""
    sent: PlannedBroadcast = _new_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    sent.is_form_sent = True
    sent.form_sent_at = datetime(2026, 9, 13, 12, 0)
    failed: PlannedBroadcast = _new_key(16, 21, "ru", RU, "def456", "yyyy-yyyy-yyyy-yyyy-yyyy")
    failed.last_error = "transportFailed: HTTP 503"
    failed.should_send_key = True
    kept: PlannedBroadcast = _confirmed(_found_key(17, 19, "uk", UA, "ghi789", "zzzz-zzzz-zzzz-zzzz-zzzz"))
    bootstrap: PlannedBroadcast = _confirmed(_found_key(17, 21, "uk", UA, "jkl345", "vvvv-vvvv-vvvv-vvvv-vvvv"),
                                             is_bootstrap=True)
    unknown: PlannedBroadcast = _found_key(17, 22, "uk", UA, "mno678", "uuuu-uuuu-uuuu-uuuu-uuuu")
    blocked: PlannedBroadcast = _not_admitted(18, 20, "en", UA, "jkl012", "wwww-wwww-wwww-wwww-wwww")
    items = (sent, kept, failed, blocked, bootstrap, unknown)
    text: str = render_keys_file([key_row_from_planned(item) for item in items], "13-09-2026 12:00")
    lines: list[str] = text.splitlines()
    quoted: list[str] = [line.split("«")[1].split("»")[0] for line in lines[: len(msg.KEYS_FILE_HEADER)][3:]]
    form_values: list[str] = [line.removeprefix("  форма  ") for line in lines if line.startswith("  форма  ")]
    assert len(quoted) == len(form_values) == 6
    # каждой строке «форма» — ровно одна цитата шапки (самая длинная подходящая) и наоборот
    matched: list[str] = [max((lead for lead in quoted if value.startswith(lead)), key=len) for value in form_values]
    assert sorted(matched) == sorted(quoted)
