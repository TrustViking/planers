from __future__ import annotations

import json

import pytest

from app.records.slot_record import ConfirmedAnswer, RecordResults, RecordSnapshot, SlotRecord, SlotStage

KEY: str = "abcd-abcd-abcd-abcd-abcd"
FORM_URL: str = "https://docs.google.com/forms/d/e/ABC/formResponse"
ANSWERS: tuple[ConfirmedAnswer, ...] = (
    ConfirmedAnswer("entry.1", "Язык стрима ( Language of stream)", "Русский ( Russian)"),
    ConfirmedAnswer("entry.2", "Название канала ( Channel name)", "Канал RU"),
    ConfirmedAnswer("entry.5", "You Tube Stream Key", KEY),
)


def _record(results: RecordResults) -> SlotRecord:
    return SlotRecord(
        slot_id="17-03-2027_1900_ru",
        youtube_channel_id="UC123",
        slot_start_utc="2027-03-17T17:00:00+00:00",
        stage=SlotStage.KEY_CONFIRMED,
        updated_at="16-03-2027 12:00",
        results=results,
        snapshot=RecordSnapshot(
            date="17-03-2027", time="19:00", language="ru", account_name="Канал RU", handle="@KanalRU",
            title="Эфир", form_url="https://forms.gle/x", decision="create", warnings=("thumbnail:forbidden",),
        ),
    )


def _row(record: SlotRecord, record_json: str | None = None) -> SlotRecord:
    return SlotRecord.from_row(
        record.slot_id,
        record.youtube_channel_id,
        record.slot_start_utc,
        record.stage.value,
        record.updated_at,
        record_json if record_json is not None else record.record_json(),
    )


def test_json_round_trip_keeps_results_and_drops_snapshot() -> None:
    results: RecordResults = RecordResults(
        broadcast_id="B1", broadcast_url="https://www.youtube.com/watch?v=B1", stream_id="S1",
        stream_url="rtmp://a.rtmp.youtube.com/live2", stream_key=KEY, published_at="16-03-2027 12:00",
        confirmed_stream_key=KEY, confirmed_form_url=FORM_URL, confirmed_answers=ANSWERS,
        confirmed_at="16-03-2027 12:01", is_bootstrap=True,
    )
    record: SlotRecord = _record(results)
    payload = json.loads(record.record_json())
    assert payload["schema"] == 1 and payload["snapshot"]["account_name"] == "Канал RU"
    assert payload["results"]["stream_key"] == KEY                    # ключ — полностью
    restored: SlotRecord = _row(record)
    assert restored.results == results
    assert (restored.stage, restored.snapshot) == (SlotStage.KEY_CONFIRMED, None)


def test_unknown_fields_are_ignored_and_missing_are_none() -> None:
    record_json: str = json.dumps(
        {"schema": 7, "future": 1, "results": {"stream_key": KEY, "new_field": "x", "confirmed_at": 5}}
    )
    restored: SlotRecord = _row(_record(RecordResults()), record_json)
    assert restored.results == RecordResults(stream_key=KEY)          # число вместо строки — None


@pytest.mark.parametrize("record_json", ["", "не json", "[1, 2]", '{"results": "x"}'])
def test_unreadable_json_gives_empty_results(record_json: str) -> None:
    assert _row(_record(RecordResults()), record_json).results == RecordResults()


def test_unknown_stage_reads_as_admitted() -> None:
    record: SlotRecord = _record(RecordResults())
    assert SlotRecord.from_row("s", "c", "t", "future_stage", "u", record.record_json()).stage is SlotStage.ADMITTED


def test_has_confirmed_compares_key_form_and_answers() -> None:
    results: RecordResults = RecordResults(confirmed_stream_key=KEY, confirmed_form_url=FORM_URL, confirmed_answers=ANSWERS)
    assert results.has_confirmed(KEY, FORM_URL, tuple(reversed(ANSWERS)))        # порядок не важен
    renamed: tuple[ConfirmedAnswer, ...] = (ANSWERS[0], ConfirmedAnswer("entry.2", "другой вопрос", "Канал RU"), ANSWERS[2])
    assert results.has_confirmed(KEY, FORM_URL, renamed)                         # название вопроса — только для людей
    assert not results.has_confirmed("wxyz-wxyz-wxyz-wxyz-wxyz", FORM_URL, ANSWERS)
    assert not results.has_confirmed(KEY, "https://docs.google.com/forms/d/e/OTHER/formResponse", ANSWERS)
    other: tuple[ConfirmedAnswer, ...] = (ANSWERS[0], ConfirmedAnswer("entry.2", "Название", "Новое имя"), ANSWERS[2])
    assert not results.has_confirmed(KEY, FORM_URL, other)
    assert not results.has_confirmed(KEY, FORM_URL, ANSWERS[:2])
    assert not RecordResults().has_confirmed(KEY, FORM_URL, ANSWERS)


def test_confirms_key_is_about_the_key_only() -> None:
    results: RecordResults = RecordResults(confirmed_stream_key=KEY)
    assert results.confirms_key(KEY)
    assert not results.confirms_key("wxyz-wxyz-wxyz-wxyz-wxyz") and not results.confirms_key(None)


def test_stage_rank_order() -> None:
    assert [stage.rank for stage in SlotStage] == [0, 1, 2]


def test_thumbnail_fact_round_trips_and_old_records_read_without_it() -> None:
    results: RecordResults = RecordResults(
        broadcast_id="B1", stream_key=KEY, thumbnail_broadcast_id="B1", thumbnail_set_at="17-09-2026 17:36"
    )
    restored: SlotRecord = _row(_record(results))
    assert (restored.results.thumbnail_broadcast_id, restored.results.thumbnail_set_at) == ("B1", "17-09-2026 17:36")
    old_json: str = json.dumps({"schema": 1, "snapshot": {}, "results": {"broadcast_id": "B1", "stream_key": KEY}})
    old: SlotRecord = _row(_record(RecordResults()), old_json)
    assert (old.results.thumbnail_broadcast_id, old.results.thumbnail_set_at) == (None, None)
    assert old.results.stream_key == KEY
