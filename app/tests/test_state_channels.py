from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.paths import PlanerPaths
from app.state.channels import BindingVerdict, ChannelBinding, ChannelBindings, ChannelsStateError

AUTHORIZED_AT: datetime = datetime(2027, 3, 16, 12, 0)   # формат планера — без секунд


def _binding(account_name: str = "Osvald.X", youtube_channel_id: str = "UC12345") -> ChannelBinding:
    return ChannelBinding(
        account_name=account_name,
        youtube_channel_id=youtube_channel_id,
        title="Тестовый канал",
        authorized_at=AUTHORIZED_AT,
    )


def test_bindings_live_in_app_state(planer_paths: PlanerPaths) -> None:
    """Технические данные — в app\\state\\, в корне планера папки state\\ нет."""
    assert planer_paths.bindings_file == planer_paths.root / "app" / "state" / "bindings.json"
    assert planer_paths.state_dir.is_dir()
    assert not (planer_paths.root / "state").exists()


def test_missing_file_gives_empty_state(planer_paths: PlanerPaths) -> None:
    assert ChannelBindings.load(planer_paths.bindings_file).bindings == {}


def test_save_and_load_round_trip(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.upsert(_binding("Канал RU", "UC67890"))
    bindings.save(planer_paths.bindings_file)

    loaded: ChannelBindings = ChannelBindings.load(planer_paths.bindings_file)
    assert loaded.get("Osvald.X") == _binding()
    assert loaded.get("Канал RU") == _binding("Канал RU", "UC67890")
    assert loaded.get("Канал XX") is None


def test_upsert_replaces_previous_binding(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.upsert(_binding(youtube_channel_id="UCother"))
    bindings.save(planer_paths.bindings_file)
    loaded: ChannelBindings = ChannelBindings.load(planer_paths.bindings_file)
    binding: ChannelBinding | None = loaded.get("Osvald.X")
    assert binding is not None and binding.youtube_channel_id == "UCother"


def test_file_is_keyed_by_account_name(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.save(planer_paths.bindings_file)
    payload: dict = json.loads(planer_paths.bindings_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["channels"] == {
        "Osvald.X": {"youtube_channel_id": "UC12345", "title": "Тестовый канал", "authorized_at": "16-03-2027 12:00"}
    }


@pytest.mark.parametrize(
    ("account_name", "youtube_channel_id", "verdict"),
    [
        ("Канал RU", "UCnew", BindingVerdict.NEW),
        ("Osvald.X", "UC12345", BindingVerdict.SAME),
        ("Osvald.X", "UCother", BindingVerdict.MISMATCH),
        ("Канал RU", "UC12345", BindingVerdict.TAKEN),
    ],
    ids=["new", "same", "mismatch", "taken_by_other_name"],
)
def test_verdict(account_name: str, youtube_channel_id: str, verdict: BindingVerdict) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    assert bindings.verdict(account_name, youtube_channel_id) is verdict


@pytest.mark.parametrize(
    ("text", "case"),
    [
        ("{broken", "broken_json"),
        ('{"schema_version": 2, "channels": {}}', "other_schema"),
        ('["not", "an", "object"]', "root_list"),
        ('{"schema_version": 1, "channels": []}', "channels_not_object"),
        ('{"schema_version": 1, "channels": {"Osvald.X": {"title": "X"}}}', "missing_fields"),
        (
            '{"schema_version": 1, "channels": {"Osvald.X": {"youtube_channel_id": "UC1",'
            ' "title": "X", "authorized_at": "2027-03-16"}}}',
            "bad_date",
        ),
    ],
)
def test_broken_state_is_rejected(planer_paths: PlanerPaths, text: str, case: str) -> None:
    planer_paths.bindings_file.parent.mkdir(parents=True, exist_ok=True)
    planer_paths.bindings_file.write_text(text, encoding="utf-8")
    with pytest.raises(ChannelsStateError):
        ChannelBindings.load(planer_paths.bindings_file)


def test_save_leaves_no_temp_files(planer_paths: PlanerPaths) -> None:
    bindings: ChannelBindings = ChannelBindings()
    bindings.upsert(_binding())
    bindings.save(planer_paths.bindings_file)
    leftovers: list[Path] = [path for path in planer_paths.state_dir.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []
