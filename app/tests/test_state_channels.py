from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.paths import PlanerPaths
from app.state.channels import ChannelBinding, ChannelBindings, ChannelsStateError

AUTHORIZED_AT: datetime = datetime(2027, 3, 16, 12, 0)   # формат планера — без секунд


def _binding(channel_key: str = "yt_ua", youtube_channel_id: str = "UC12345") -> ChannelBinding:
    return ChannelBinding(
        channel_key=channel_key,
        youtube_channel_id=youtube_channel_id,
        title="Тестовый канал",
        authorized_at=AUTHORIZED_AT,
    )


def test_missing_file_gives_empty_state(planer_paths: PlanerPaths) -> None:
    assert ChannelBindings.load(planer_paths.channels_state_file).bindings == {}


def test_save_and_load_round_trip(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.upsert(_binding("yt_ru", "UC67890"))
    bindings.save(planer_paths.channels_state_file)

    loaded: ChannelBindings = ChannelBindings.load(planer_paths.channels_state_file)
    assert loaded.get("yt_ua") == _binding()
    assert loaded.get("yt_ru") == _binding("yt_ru", "UC67890")
    assert loaded.get("yt_nope") is None


def test_upsert_replaces_previous_binding(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.upsert(_binding(youtube_channel_id="UCother"))
    bindings.save(planer_paths.channels_state_file)
    loaded: ChannelBindings = ChannelBindings.load(planer_paths.channels_state_file)
    binding: ChannelBinding | None = loaded.get("yt_ua")
    assert binding is not None and binding.youtube_channel_id == "UCother"


def test_dates_are_written_in_planer_format(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.save(planer_paths.channels_state_file)
    payload: dict = json.loads(planer_paths.channels_state_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["channels"]["yt_ua"]["authorized_at"] == "16-03-2027 12:00"


@pytest.mark.parametrize(
    ("text", "case"),
    [
        ("{broken", "broken_json"),
        ('{"schema_version": 2, "channels": {}}', "other_schema"),
        ('["not", "an", "object"]', "root_list"),
        ('{"schema_version": 1, "channels": []}', "channels_not_object"),
        ('{"schema_version": 1, "channels": {"yt_ua": {"title": "X"}}}', "missing_fields"),
        (
            '{"schema_version": 1, "channels": {"yt_ua": {"youtube_channel_id": "UC1",'
            ' "title": "X", "authorized_at": "2027-03-16"}}}',
            "bad_date",
        ),
    ],
)
def test_broken_state_is_rejected(planer_paths: PlanerPaths, text: str, case: str) -> None:
    planer_paths.channels_state_file.parent.mkdir(parents=True, exist_ok=True)
    planer_paths.channels_state_file.write_text(text, encoding="utf-8")
    with pytest.raises(ChannelsStateError):
        ChannelBindings.load(planer_paths.channels_state_file)


def test_save_leaves_no_temp_files(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.save(planer_paths.channels_state_file)
    leftovers: list[Path] = [path for path in planer_paths.state_dir.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []
