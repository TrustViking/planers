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
from app.pipeline.plan import PlannedBroadcast
from app.pipeline.reconciler import MarkedBroadcast, MarkerParts
from app.platforms.base import CreatedBroadcast, StreamInfo, UpcomingBroadcast
from app.tests.conftest import build_planned, build_slot

KYIV: timezone = timezone(timedelta(hours=2))
STREAM_URL: str = "rtmp://a.rtmp.youtube.com/live2"

# Образец ТЗ §5.5: блок на стрим, ключ — первой строкой блока. Статус формы говорит только
# об этом запуске — передан сейчас, НЕ передан новый ключ, или ключ прежний.
TZ_SAMPLE_KEYS: str = """# Ключи трансляций. Сгенерировано планером 13-09-2026 12:00.
# Файл перезаписывается на каждом запуске — не править.
# Если в строке «форма» стоит «НЕ передан» — передайте ключ стримеру вручную.

16-09-2026 19:00  uk  Іван UA
  ключ   xxxx-xxxx-xxxx-xxxx-xxxx
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=abc123
  форма  передан 13-09-2026 12:00

16-09-2026 21:00  ru  Иван RU
  ключ   yyyy-yyyy-yyyy-yyyy-yyyy
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=def456
  форма  НЕ передан — форма недоступна (HTTP 503)

17-09-2026 19:00  uk  Іван UA
  ключ   zzzz-zzzz-zzzz-zzzz-zzzz
  поток  rtmp://a.rtmp.youtube.com/live2
  эфир   https://www.youtube.com/watch?v=ghi789
  форма  ключ прежний, в этом запуске не передавался
"""


def _channel(channel_id: str, account_name: str, language: str) -> ChannelConfig:
    return ChannelConfig(
        id=channel_id,
        platform=Platform.YOUTUBE,
        account_name=account_name,
        languages=(language,),
        privacy=Privacy.PUBLIC,
        auto_start=True,
        set_thumbnail=True,
        category_id="22",
    )


UA: ChannelConfig = _channel("yt_ua", "Іван UA", "uk")
RU: ChannelConfig = _channel("yt_ru", "Иван RU", "ru")


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
    kept: PlannedBroadcast = _found_key(17, 19, "uk", UA, "ghi789", "zzzz-zzzz-zzzz-zzzz-zzzz")
    rows = [key_row_from_planned(kept), key_row_from_planned(failed), key_row_from_planned(sent)]
    assert render_keys_file(rows, "13-09-2026 12:00") == TZ_SAMPLE_KEYS


def test_row_takes_everything_from_the_object() -> None:
    """Дата, время, язык и аккаунт — из слота и канала; ключ и ссылка — с площадки."""
    item: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    row = key_row_from_planned(item)
    assert (row.date, row.time, row.language, row.account_name) == ("16-09-2026", "19:00", "uk", "Іван UA")
    assert (row.stream_key, row.broadcast_url) == ("xxxx-xxxx-xxxx-xxxx-xxxx", "https://www.youtube.com/watch?v=abc123")


def test_object_without_key_shows_dashes() -> None:
    item: PlannedBroadcast = build_planned(build_slot(_start(16, 19), "uk"), UA)
    row = key_row_from_planned(item)
    assert (row.stream_key, row.stream_url, row.broadcast_url) == ("-", "-", "-")


def test_form_status_texts_speak_only_about_this_run() -> None:
    new: PlannedBroadcast = _new_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    new.last_error = "notConfirmed: HTTP 200"
    assert form_status_text(new) == "НЕ передан — форма не подтвердила запись ответа (HTTP 200)"
    new.is_form_sent = True
    new.form_sent_at = datetime(2026, 9, 13, 12, 0)
    assert form_status_text(new) == "передан 13-09-2026 12:00"
    kept: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    assert form_status_text(kept) == "ключ прежний, в этом запуске не передавался"


def test_status_row_does_not_look_into_the_journal() -> None:
    marked: MarkedBroadcast = MarkedBroadcast(
        channel=UA,
        broadcast=UpcomingBroadcast("abc123", _start(16, 19), "Эфир", "", "s1"),
        stream=StreamInfo("s1", "16-09-2026_1900_uk", STREAM_URL, "xxxx-xxxx-xxxx-xxxx-xxxx"),
        parts=MarkerParts(date="16-09-2026", time="19:00", language="uk"),
    )
    row = key_row_from_marked(marked)
    assert (row.form_status_text, row.stream_key) == ("ключ прежний, в этом запуске не передавался", "xxxx-xxxx-xxxx-xxxx-xxxx")


def test_empty_file_has_only_comment_lines(planer_paths: PlanerPaths) -> None:
    path: Path = write_keys_file(planer_paths, render_keys_file([], "16-03-2027 12:00"))
    assert path == planer_paths.keys_file
    lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and all(line.startswith("# ") for line in lines)


def test_key_is_the_first_line_of_each_block() -> None:
    """Ключ — ради него файл и открывают — стоит первым в блоке, а не в конце длинной строки."""
    item: PlannedBroadcast = _found_key(16, 19, "uk", UA, "abc123", "xxxx-xxxx-xxxx-xxxx-xxxx")
    lines: list[str] = render_keys_file([key_row_from_planned(item)], "13-09-2026 12:00").splitlines()
    assert lines[3] == ""
    assert lines[4] == "16-09-2026 19:00  uk  Іван UA"
    assert lines[5] == "  ключ   xxxx-xxxx-xxxx-xxxx-xxxx"
    assert all(len(line) <= 80 for line in lines[4:])
