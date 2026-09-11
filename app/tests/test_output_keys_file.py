from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.output.keys_file import form_status_text, key_row_from_registration, render_keys_file, write_keys_file
from app.paths import PlanerPaths
from app.state.registry import FormStatus, Registration

# Образец ТЗ §5.5 с ключами по 5 групп. Отличие от ТЗ: в строке «форма ❌ ошибка сети» один
# пробел перед « | », в ТЗ — два (опечатка выравнивания; поля всегда разделяются « | »).
TZ_SAMPLE_KEYS: str = """# Ключи трансляций. Сгенерировано планером 13-09-2026 12:00. Файл производный — не править.
# язык | дата | время (Киев) | аккаунт | статус формы | stream_url | stream_key | ссылка на эфир
uk | 16-09-2026 | 19:00 | Іван UA | форма ✅ 13-09-2026 12:00 | rtmp://a.rtmp.youtube.com/live2 | xxxx-xxxx-xxxx-xxxx-xxxx | https://www.youtube.com/watch?v=abc123
ru | 16-09-2026 | 21:00 | Иван RU | форма ❌ ошибка сети | rtmp://a.rtmp.youtube.com/live2 | yyyy-yyyy-yyyy-yyyy-yyyy | https://www.youtube.com/watch?v=def456
"""


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


def test_render_matches_tz_sample() -> None:
    failed: Registration = _registration(
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
    )
    rows = [key_row_from_registration(failed), key_row_from_registration(_registration())]
    assert render_keys_file(rows, "13-09-2026 12:00") == TZ_SAMPLE_KEYS


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
