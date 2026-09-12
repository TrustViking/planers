from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config.loader import ChannelConfig, Platform, Privacy
from app.output.keys_file import form_status_text, key_row_from_planned, render_keys_file, write_keys_file
from app.paths import PlanerPaths
from app.pipeline.plan import PlannedBroadcast
from app.state.registry import FormStatus, Registration
from app.tests.conftest import build_planned, build_slot

KYIV: timezone = timezone(timedelta(hours=2))

# Образец ТЗ §5.5 с ключами по 5 групп. Отличие от ТЗ: в строке «форма ❌ ошибка сети» один
# пробел перед « | », в ТЗ — два (опечатка выравнивания; поля всегда разделяются « | »).
TZ_SAMPLE_KEYS: str = """# Ключи трансляций. Сгенерировано планером 13-09-2026 12:00. Файл производный — не править.
# язык | дата | время (Киев) | аккаунт | статус формы | stream_url | stream_key | ссылка на эфир
uk | 16-09-2026 | 19:00 | Іван UA | форма ✅ 13-09-2026 12:00 | rtmp://a.rtmp.youtube.com/live2 | xxxx-xxxx-xxxx-xxxx-xxxx | https://www.youtube.com/watch?v=abc123
ru | 16-09-2026 | 21:00 | Иван RU | форма ❌ ошибка сети | rtmp://a.rtmp.youtube.com/live2 | yyyy-yyyy-yyyy-yyyy-yyyy | https://www.youtube.com/watch?v=def456
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


def _registration(**overrides: Any) -> Registration:
    values: dict[str, Any] = dict(
        slot_id="16-09-2026_1900_uk",
        channel_id="yt_ua",
        account_name="Іван UA",
        language="uk",
        date="16-09-2026",
        time="19:00",
        broadcast_id="abc123",
        broadcast_url="https://www.youtube.com/watch?v=abc123",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="xxxx-xxxx-xxxx-xxxx-xxxx",
        package_id="20260913-101502-a1b2c3",
        created_at=datetime(2026, 9, 13, 12, 0),
        form_status=FormStatus.SENT,
        form_sent_at=datetime(2026, 9, 13, 12, 0),
        previous_broadcast_ids=[],
        last_error=None,
    )
    values.update(overrides)
    return Registration(**values)


def _planned(hour: int, language: str, channel: ChannelConfig, registration: Registration) -> PlannedBroadcast:
    item: PlannedBroadcast = build_planned(
        build_slot(datetime(2026, 9, 16, hour, 0, tzinfo=KYIV), language),
        channel,
    )
    item.apply_registration(registration)
    return item


def test_render_matches_tz_sample() -> None:
    sent: PlannedBroadcast = _planned(19, "uk", _channel("yt_ua", "Іван UA", "uk"), _registration())
    failed: PlannedBroadcast = _planned(
        21,
        "ru",
        _channel("yt_ru", "Иван RU", "ru"),
        _registration(
            slot_id="16-09-2026_2100_ru",
            channel_id="yt_ru",
            account_name="Иван RU",
            language="ru",
            time="21:00",
            broadcast_id="def456",
            broadcast_url="https://www.youtube.com/watch?v=def456",
            stream_key="yyyy-yyyy-yyyy-yyyy-yyyy",
            form_status=FormStatus.PENDING,
            form_sent_at=None,
            last_error="ошибка сети",
        ),
    )
    rows = [key_row_from_planned(failed), key_row_from_planned(sent)]
    assert render_keys_file(rows, "13-09-2026 12:00") == TZ_SAMPLE_KEYS


def test_row_takes_texts_from_the_object_not_from_the_journal() -> None:
    """Дата, время, язык и аккаунт — из слота и канала объекта; журнал даёт ключи и статус формы."""
    channel: ChannelConfig = _channel("yt_ua", "Іван UA", "uk")
    item: PlannedBroadcast = _planned(19, "uk", channel, _registration(account_name="Старое имя", date="01-01-2000"))
    row = key_row_from_planned(item)
    assert (row.date, row.time, row.language, row.account_name) == ("16-09-2026", "19:00", "uk", "Іван UA")
    assert row.stream_key == "xxxx-xxxx-xxxx-xxxx-xxxx"


def test_object_without_key_shows_dashes() -> None:
    item: PlannedBroadcast = build_planned(
        build_slot(datetime(2026, 9, 16, 19, 0, tzinfo=KYIV), "uk"),
        _channel("yt_ua", "Іван UA", "uk"),
    )
    row = key_row_from_planned(item)
    assert (row.stream_key, row.stream_url, row.broadcast_url) == ("-", "-", "-")


def test_form_status_texts() -> None:
    assert form_status_text(_registration()) == "форма ✅ 13-09-2026 12:00"
    assert form_status_text(_registration(form_status=FormStatus.PENDING, form_sent_at=None)) == (
        "форма ⏳ отправка появится на этапе 4"
    )
    assert form_status_text(None) == "форма — не отправлялась"


def test_empty_file_has_only_comment_lines(planer_paths: PlanerPaths) -> None:
    path: Path = write_keys_file(planer_paths, render_keys_file([], "16-03-2027 12:00"))
    assert path == planer_paths.keys_file
    lines: list[str] = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.startswith("# ") for line in lines)
