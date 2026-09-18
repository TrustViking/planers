from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, PlanerConfig
from app.core.dates import format_datetime_text
from app.package.model import Slot
from app.pipeline.plan import (
    FIXABLE_FIELDS,
    REPORTED_FIELDS,
    SPEC_ATTRIBUTES,
    BroadcastSpec,
    ChangedField,
    Decision,
    OutcomeError,
    PlannedBroadcast,
    AdmissionKind,
    AdmissionReason,
)
from app.form.base import FormError
from app.form.key_form import KeyForm
from app.platforms.channel import Channel, ChannelStatus
from app.records.slot_record import SlotRecord, SlotStage
from app.tests.test_form_key_form import training_key_form
from app.ui import messages_ru as msg
from app.platforms.base import BroadcastFacts, CreatedBroadcast, PlatformLimits, StreamInfo, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.tests.conftest import build_config, build_planned, build_slot

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]
LIMITS: PlatformLimits = PlatformLimits(
    title_max_chars=20,
    description_max_chars=40,
    auto_stop=True,
    latency_preference="normal",
)
CHANNEL: ChannelConfig = build_config().channels[0]
SETTINGS = build_config().settings
CREATED: CreatedBroadcast = CreatedBroadcast(
    broadcast_id="newbc",
    broadcast_url="https://www.youtube.com/watch?v=newbc",
    stream_id="S9",
    stream_url="rtmp://a.rtmp.youtube.com/live2",
    stream_key="newk-newk-newk-newk-newk",
)


def _expected(slot: Slot) -> BroadcastSpec:
    return BroadcastSpec.from_slot(slot, LIMITS, CHANNEL, SETTINGS)


def _broadcast(
    title: str,
    description: str,
    start: datetime,
    stream_id: str | None = "S1",
    **overrides: Any,
) -> UpcomingBroadcast:
    """Эфир так, как его вернул бы список YouTube для эфира, поставленного планером с build_config."""
    values: dict[str, Any] = dict(
        broadcast_id="B1",
        start_utc=start,
        title=title,
        description=description,
        stream_id=stream_id,
        privacy_status=CHANNEL.privacy.value,
        auto_start=SETTINGS.auto_start,
        auto_stop=LIMITS.auto_stop,
        latency_preference=LIMITS.latency_preference,
    )
    values.update(overrides)
    return UpcomingBroadcast(**values)


def _stream(title: str) -> StreamInfo:
    return StreamInfo(
        stream_id="S1",
        title=title,
        ingestion_address="rtmp://a.rtmp.youtube.com/live2",
        stream_name="abcd-abcd-abcd-abcd-abcd",
    )


def test_spec_from_slot_normalizes_and_trims(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(
        now + timedelta(days=1),
        "uk",
        title="  Очень длинное название эфира на канале  ",
        description="Первый абзац  \r\n\r\nВторой  ",
    )
    spec: BroadcastSpec = _expected(slot)
    assert len(spec.title) <= LIMITS.title_max_chars
    assert spec.title == spec.title.strip()
    assert spec.description == "Первый абзац\n\nВторой"
    assert spec.marker == slot.slot_id
    assert spec.start_minute.tzinfo is timezone.utc
    assert (spec.start_minute.second, spec.start_minute.microsecond) == (0, 0)


def test_spec_from_slot_takes_every_dictated_value_from_its_source(make_slot_object: SlotFactory, now: datetime) -> None:
    """Видимость — из канала, категория и автостарт — из planer.json, автостоп и задержка — от площадки."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    settings = build_config(auto_start=False, category_id="25").settings
    spec: BroadcastSpec = BroadcastSpec.from_slot(slot, LIMITS, CHANNEL, settings)
    assert (spec.privacy, spec.category_id, spec.auto_start) == (CHANNEL.privacy.value, "25", False)
    assert (spec.auto_stop, spec.latency_preference) == (LIMITS.auto_stop, LIMITS.latency_preference)
    # слот без превью: обложку планер не ставит — и не сверяет; остальные диктуемые поля заполнены
    assert spec.has_own_thumbnail is None
    assert all(spec.value(name) is not None for name in ChangedField if name is not ChangedField.THUMBNAIL)
    # слот с превью: обложка из пакета должна стоять
    with_preview: Slot = replace(slot, previews=(f"previews/{slot.slot_id}_1.jpg",))
    assert BroadcastSpec.from_slot(with_preview, LIMITS, CHANNEL, settings).has_own_thumbnail is True


def test_spec_from_platform_applies_the_same_rules(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Эфир", description="Текст")
    expected: BroadcastSpec = _expected(slot)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast("  Эфир  ", "Текст  \r\n", slot.start, category_id=SETTINGS.category_id),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual == expected
    assert actual.diff(expected) == ()


def test_spec_from_platform_without_stream_has_empty_marker(
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    spec: BroadcastSpec = BroadcastSpec.from_platform(_broadcast("Эфир", "", slot.start, None), None, LIMITS)
    assert spec.marker == ""


def test_diff_reports_both_texts(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Новое", description="Новое описание")
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast("Старое", "Старое описание", slot.start),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual.diff(_expected(slot)) == (ChangedField.TITLE, ChangedField.DESCRIPTION)


@pytest.mark.parametrize(
    ("overrides", "marker", "changed"),
    [
        ({"privacy_status": "private"}, None, ChangedField.PRIVACY),
        ({"category_id": "24"}, None, ChangedField.CATEGORY),
        ({}, "Мой ключ", ChangedField.MARKER),
        ({"auto_start": False}, None, ChangedField.AUTO_START),
        ({"auto_stop": False}, None, ChangedField.AUTO_STOP),
        ({"latency_preference": "low"}, None, ChangedField.LATENCY),
    ],
    ids=["privacy", "category", "marker", "auto_start", "auto_stop", "latency"],
)
def test_diff_covers_every_dictated_field(
    make_slot_object: SlotFactory,
    now: datetime,
    overrides: dict[str, Any],
    marker: str | None,
    changed: ChangedField,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    expected: BroadcastSpec = _expected(slot)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast(expected.title, expected.description, slot.start, **overrides),
        _stream(marker or slot.slot_id),
        LIMITS,
    )
    assert actual.diff(expected) == (changed,)


def test_diff_ignores_time_and_unreported_values(make_slot_object: SlotFactory, now: datetime) -> None:
    """По времени эфир опознают; поле, которое площадка не вернула (None), сравнивать не с чем."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    expected: BroadcastSpec = _expected(slot)
    other: BroadcastSpec = replace(
        expected,
        start_minute=expected.start_minute + timedelta(days=5),
        category_id=None,
        privacy=None,
        auto_start=None,
    )
    assert other.diff(expected) == ()
    # но пропажа поля не беззвучна: not_compared называет, по чему сверки не было
    # слот без превью: обложка не сверяется ни у одной стороны
    assert other.not_compared(expected) == (
        ChangedField.CATEGORY,
        ChangedField.PRIVACY,
        ChangedField.THUMBNAIL,
        ChangedField.AUTO_START,
    )
    assert expected.not_compared(expected) == (ChangedField.THUMBNAIL,)


def test_fields_are_split_once_and_completely() -> None:
    """Каждое сверяемое поле либо исправляется, либо только сообщается — третьего нет."""
    assert FIXABLE_FIELDS | REPORTED_FIELDS == frozenset(ChangedField)
    assert not FIXABLE_FIELDS & REPORTED_FIELDS
    assert set(SPEC_ATTRIBUTES) == set(ChangedField)


def test_spec_from_facts_uses_expected_start_when_platform_has_none(make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    expected: BroadcastSpec = _expected(slot)
    facts: BroadcastFacts = BroadcastFacts(
        broadcast_id="B1", title=expected.title, description=expected.description, start_utc=None,
        privacy_status="private", made_for_kids=False, age_restricted=False, default_language="uk",
        default_audio_language="uk", category_id="22", bound_stream_id="S1", stream_marker=slot.slot_id,
        auto_start=True, auto_stop=True, latency_preference="normal",
    )
    spec: BroadcastSpec = BroadcastSpec.from_facts(facts, LIMITS, expected.start_minute)
    assert spec.start_minute == expected.start_minute
    assert spec.diff(expected) == (ChangedField.PRIVACY,)


def test_long_title_is_trimmed_on_both_sides(make_slot_object: SlotFactory, now: datetime) -> None:
    """Слот длиннее лимита не должен считаться отличающимся от того, что лежит на площадке."""
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Очень длинное название эфира " * 5)
    expected: BroadcastSpec = _expected(slot)
    actual: BroadcastSpec = BroadcastSpec.from_platform(
        _broadcast(expected.title, slot.description, slot.start),
        _stream(slot.slot_id),
        LIMITS,
    )
    assert actual.diff(expected) == ()


def test_split_changed_separates_fixable_and_reported(
    make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime,
) -> None:
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    item.split_changed((ChangedField.PRIVACY, ChangedField.AUTO_START, ChangedField.MARKER))
    assert item.changed_fields == (ChangedField.PRIVACY, ChangedField.MARKER)
    assert item.reported_fields == (ChangedField.AUTO_START,)


def test_fields_come_from_slot_and_channel(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    item: PlannedBroadcast = build_planned(slot, make_config().channels[0])
    assert (item.slot_id, item.language, item.date, item.time) == (slot.slot_id, "uk", slot.date, slot.time)
    assert item.account_name == "yt_ua"
    assert item.form is slot.form


def _with_found_key(item: PlannedBroadcast) -> PlannedBroadcast:
    item.found = _broadcast(item.slot.title, item.slot.description, item.slot.start)
    item.found_stream = _stream(item.slot_id)
    item.take_found_key()
    return item


def test_object_is_born_without_key(make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    """Объект знает только пакет и канал: о прошлых запусках ему нечего помнить."""
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    assert (item.broadcast_id, item.broadcast_url, item.stream_url, item.stream_key) == (None, None, None, None)
    assert (item.should_send_key, item.is_form_sent, item.is_key_undelivered) == (False, False, False)


def test_found_key_is_taken_from_the_platform(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = _with_found_key(
        build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    )
    assert (item.broadcast_id, item.stream_key) == ("B1", "abcd-abcd-abcd-abcd-abcd")
    assert (item.broadcast_url, item.stream_url) == ("https://www.youtube.com/watch?v=B1", "rtmp://a.rtmp.youtube.com/live2")
    assert item.should_send_key is False               # найденный ключ сам по себе в форму не идёт
    assert item.is_key_undelivered is False


def test_new_key_waits_for_the_form(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    item.take_new_key(CREATED)
    assert (item.broadcast_id, item.stream_id, item.stream_key, item.should_send_key) == (
        "newbc", "S9", CREATED.stream_key, False
    )
    item.decide_key_delivery()
    assert item.should_send_key is True and item.is_key_undelivered is True
    item.is_form_sent = True
    assert item.is_key_undelivered is False


def test_kept_key_needs_a_confirmation_of_the_current_key(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    item: PlannedBroadcast = _with_found_key(
        build_planned(make_slot_object(now + timedelta(days=1), "uk"), make_config().channels[0])
    )
    assert item.has_kept_key is False                  # памяти о ключе нет
    item.confirm_key(now)
    assert item.has_kept_key is True
    item.error = OutcomeError(origin="youtube", code="forbidden")
    assert item.has_kept_key is False                  # ошибка — это не прежний ключ
    item.error = None
    item.take_new_key(CREATED)                         # привязка потока: ключ новый
    assert item.has_kept_key is False


def test_package_fields_survive_platform_data(
    make_config: ConfigFactory,
    make_slot_object: SlotFactory,
    now: datetime,
) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    channel: Any = make_config().channels[0]
    item: PlannedBroadcast = build_planned(slot, channel)
    expected: BroadcastSpec = item.expected
    item.found = _broadcast("Чужое", "Чужое", slot.start)
    item.actual = BroadcastSpec.from_platform(item.found, _stream("маркер"), FakePlatform().limits)
    assert item.slot is slot
    assert item.channel is channel
    assert item.expected is expected
    assert item.source_package.package_id == "pkg-test"


# --- допуск к публикации (PlannedBroadcast.admit)

TRAINING_DAY: datetime = datetime(2026, 9, 13, 19, 0, tzinfo=timezone(timedelta(hours=3)))


def _admission_item(language: str = "uk", start: datetime = TRAINING_DAY) -> PlannedBroadcast:
    config: PlanerConfig = build_config(channels=(("yt_all", ["uk", "ru", "en", "hu"]),))
    return build_planned(build_slot(start, language), config.channels[0])


def _channel_object(item: PlannedBroadcast, status: ChannelStatus) -> Channel:
    return Channel(config=item.channel, token_file=Path("t.json"), status=status)


def test_ready_channel_and_complete_form_admit_the_object(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item()
    item.admit(_channel_object(item, ChannelStatus.READY), training_key_form(tmp_path), None)
    assert item.is_admitted and item.decision is Decision.CREATE
    assert item.form_answers is not None and item.form_answers.missing == ()
    assert item.form_answers.pending == ("You Tube Stream Key", "Stream-URL (YT)")   # ключ и адрес — не причина


@pytest.mark.parametrize("status", [ChannelStatus.REFUSED, ChannelStatus.FAILED, ChannelStatus.NEEDS_LOGIN])
def test_channel_not_ready_is_a_reason(tmp_path: Path, status: ChannelStatus) -> None:
    item: PlannedBroadcast = _admission_item()
    item.admit(_channel_object(item, status), training_key_form(tmp_path), None)
    assert item.decision is Decision.NOT_ADMITTED and not item.is_admitted
    assert item.admission_reasons == (
        AdmissionReason(AdmissionKind.CHANNEL, status.value, None, msg.ADMISSION_CHANNEL_TEXT[status.value]),
    )


def test_unreadable_form_is_a_reason() -> None:
    item: PlannedBroadcast = _admission_item()
    item.admit(None, None, FormError("structureUnreadable", "FB_PUBLIC_LOAD_DATA_ not found"))
    assert item.admission_reasons == (
        AdmissionReason(AdmissionKind.FORM_UNREADABLE, "structureUnreadable", None, "FB_PUBLIC_LOAD_DATA_ not found"),
    )
    assert item.decision is Decision.NOT_ADMITTED


def test_date_without_option_is_a_form_field_reason(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item("en", datetime(2027, 3, 18, 20, 0, tzinfo=timezone(timedelta(hours=2))))
    item.admit(_channel_object(item, ChannelStatus.READY), training_key_form(tmp_path), None)
    assert item.admission_reasons == (
        AdmissionReason(
            AdmissionKind.FORM_FIELD, "missingOption", "date", "Время стрима ( Stream time ): 18.03.2027",
            question="Время стрима ( Stream time )", value="18.03.2027",
        ),
    )
    assert item.decision is Decision.NOT_ADMITTED


def test_language_without_option_is_a_form_field_reason(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item("hu")
    item.admit(None, training_key_form(tmp_path), None)
    assert [(reason.kind, reason.code, reason.field) for reason in item.admission_reasons] == [
        (AdmissionKind.FORM_FIELD, "missingOption", "language")
    ]


def test_reasons_keep_their_order(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item("hu", datetime(2027, 3, 18, 20, 0, tzinfo=timezone(timedelta(hours=2))))
    item.admit(_channel_object(item, ChannelStatus.FAILED), training_key_form(tmp_path), None)
    assert [reason.kind for reason in item.admission_reasons] == [
        AdmissionKind.CHANNEL, AdmissionKind.FORM_FIELD, AdmissionKind.FORM_FIELD
    ]


def test_too_late_object_gets_fields_but_no_reasons(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item("en", datetime(2027, 3, 18, 20, 0, tzinfo=timezone(timedelta(hours=2))))
    item.is_too_late = True
    item.decision = Decision.TOO_LATE
    form: KeyForm = training_key_form(tmp_path)
    item.admit(_channel_object(item, ChannelStatus.REFUSED), form, None)
    assert item.is_admitted and item.decision is Decision.TOO_LATE
    assert item.key_form is form and item.channel_object is not None


def test_refreshed_answers_with_key_and_url_are_complete(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item()
    item.admit(None, training_key_form(tmp_path), None)
    item.take_new_key(CREATED)
    assert not item.is_key_ready_to_send                  # решения ещё нет, ответы без ключа и адреса
    item.decide_key_delivery()
    answers = item.form_answers
    assert answers is not None and answers.is_complete
    assert item.is_key_ready_to_send
    item.is_form_sent = True
    assert not item.is_key_ready_to_send


def test_stream_url_not_in_options_makes_answers_incomplete(tmp_path: Path) -> None:
    item: PlannedBroadcast = _admission_item()
    item.admit(None, training_key_form(tmp_path), None)
    item.take_new_key(replace(CREATED, stream_url="rtmp://b.rtmp.youtube.com/live2"))
    answers = item.refresh_form_answers()
    assert answers is not None and not answers.is_complete
    assert [missing.field for missing in answers.missing] == ["stream_url"]
    assert not item.is_key_ready_to_send
    assert item.is_admitted                               # допуск уже решён; адрес — забота отправки


def test_object_built_directly_is_admitted_without_form() -> None:
    item: PlannedBroadcast = _admission_item()
    item.take_new_key(CREATED)
    item.decide_key_delivery()
    assert item.is_admitted and item.form_answers is None and item.is_key_ready_to_send


# --- память планера: одно правило отправки ключа (decide_key_delivery)

MEMORY_NOW: datetime = datetime(2026, 9, 13, 12, 0, tzinfo=timezone(timedelta(hours=3)))
MEMORY_CREATED: datetime = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
BEFORE_MEMORY: datetime = MEMORY_CREATED - timedelta(days=1)
FOUND_KEY: str = "fkey-fkey-fkey-fkey-fkey"


def _memory_item(tmp_path: Path, *, found: bool = True, decision: Decision = Decision.MATCH) -> PlannedBroadcast:
    """Допущенный объект тренировочной формы; found — эфир с меткой планера уже стоит на канале."""
    item: PlannedBroadcast = _admission_item()
    item.admit(None, training_key_form(tmp_path), None)
    if found:
        item.found = UpcomingBroadcast("fbc", item.slot.start, item.slot.title, "", "fs", published_utc=BEFORE_MEMORY)
        item.found_stream = StreamInfo("fs", item.slot_id, "rtmp://a.rtmp.youtube.com/live2", FOUND_KEY)
        item.take_found_key()
        item.decision = decision
    return item


def _remember(item: PlannedBroadcast) -> PlannedBroadcast:
    """Прошлый запуск: форма подтвердила текущую тройку объекта — запись в памяти."""
    item.refresh_form_answers()
    item.confirm_key(MEMORY_NOW)
    item.record = item.to_record(MEMORY_NOW, SlotStage.KEY_CONFIRMED)
    item.confirmed_results = None
    return item


def _decided(item: PlannedBroadcast) -> bool:
    item.decide_key_delivery()
    return item.should_send_key


def test_created_key_goes(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path, found=False)
    item.take_new_key(CREATED)
    assert _decided(item)


def test_matched_and_confirmed_key_does_not_go(tmp_path: Path) -> None:
    assert not _decided(_remember(_memory_item(tmp_path)))


def test_matched_without_confirmation_goes(tmp_path: Path) -> None:
    """Обрыв, прошлая отправка не прошла или объект не был допущен — подтверждения нет."""
    assert _decided(_memory_item(tmp_path))


def test_updated_with_the_same_answers_does_not_go(tmp_path: Path) -> None:
    item: PlannedBroadcast = _remember(_memory_item(tmp_path, decision=Decision.UPDATE))
    assert not _decided(item)


def test_updated_with_other_answers_goes(tmp_path: Path) -> None:
    """Название канала выровняли — ответ формы другой: ключ прежний, но уходит."""
    item: PlannedBroadcast = _remember(_memory_item(tmp_path, decision=Decision.UPDATE))
    item.channel = replace(item.channel, account_name="Новое название")
    assert _decided(item)


def test_recreated_broadcast_has_a_new_key_that_goes(tmp_path: Path) -> None:
    item: PlannedBroadcast = _remember(_memory_item(tmp_path))
    item.found = item.found_stream = None
    item.take_new_key(CREATED)
    assert _decided(item)


def test_other_form_address_makes_the_key_go(tmp_path: Path) -> None:
    item: PlannedBroadcast = _remember(_memory_item(tmp_path))
    item.key_form = replace(
        item.key_form, structure=replace(item.key_form.structure, response_url="https://docs.google.com/forms/d/e/BATTLE/formResponse")
    )
    assert _decided(item)


def test_too_late_and_not_admitted_keys_do_not_go(tmp_path: Path) -> None:
    late: PlannedBroadcast = _memory_item(tmp_path)
    late.is_too_late = True
    assert not _decided(late)
    blocked: PlannedBroadcast = _memory_item(tmp_path)
    blocked.admission_reasons = (AdmissionReason(AdmissionKind.FORM_FIELD, "missingOption", "date", "дата"),)
    assert not _decided(blocked)


def test_key_without_stream_url_does_not_go(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path)
    item.stream_url = None
    assert not _decided(item)


def test_error_of_another_step_does_not_cancel_the_key(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path, found=False)
    item.take_new_key(CREATED)
    item.decision = Decision.ERROR
    item.error = OutcomeError(origin="package", code="preview_missing")
    assert _decided(item)


def test_incomplete_answers_still_mean_the_key_must_go(tmp_path: Path) -> None:
    """Адреса потока нет среди вариантов: ключ должен уйти, но готов не был — «НЕ отправлен»."""
    item: PlannedBroadcast = _memory_item(tmp_path, found=False)
    item.take_new_key(replace(CREATED, stream_url="rtmp://b.rtmp.youtube.com/live2"))
    assert _decided(item) and not item.is_key_ready_to_send and item.is_key_undelivered


def test_bootstrap_confirms_marked_broadcast_without_sending(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path, decision=Decision.UPDATE)
    assert item.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)
    assert item.is_bootstrap_confirmed and item.results.is_bootstrap
    assert not _decided(item)
    assert item.to_record(MEMORY_NOW, SlotStage.ADMITTED).stage is SlotStage.KEY_CONFIRMED


def test_bootstrap_skips_unmarked_created_known_and_blocked(tmp_path: Path) -> None:
    unmarked: PlannedBroadcast = _memory_item(tmp_path)
    unmarked.found_stream = replace(unmarked.found_stream, title="ручной ключ")
    assert not unmarked.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED) and _decided(unmarked)
    created: PlannedBroadcast = _memory_item(tmp_path, found=False)
    created.take_new_key(CREATED)
    assert not created.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)
    known: PlannedBroadcast = _memory_item(tmp_path)
    known.record = replace(_remember(_memory_item(tmp_path)).record)
    assert not known.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)
    late: PlannedBroadcast = _memory_item(tmp_path)
    late.is_too_late = True
    assert not late.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)


def test_record_carries_results_and_keeps_the_confirmation(tmp_path: Path) -> None:
    item: PlannedBroadcast = _remember(_memory_item(tmp_path))
    record: SlotRecord = item.to_record(MEMORY_NOW, SlotStage.ADMITTED)
    assert record.youtube_channel_id == "yt_all"                       # без объекта канала — ключ канала (тесты)
    assert record.slot_start_utc == "2026-09-13T16:00:00+00:00"
    assert record.stage is SlotStage.KEY_CONFIRMED                     # не откатывается для того же ключа
    assert (record.results.stream_key, record.results.stream_id, record.results.confirmed_stream_key) == (
        FOUND_KEY, "fs", FOUND_KEY
    )
    assert record.snapshot is not None and record.snapshot.decision == "match"
    item.found = item.found_stream = None
    item.take_new_key(CREATED)                                         # эфир создан заново
    renewed: SlotRecord = item.to_record(MEMORY_NOW, SlotStage.PUBLISHED)
    assert renewed.stage is SlotStage.PUBLISHED and renewed.results.stream_key == CREATED.stream_key
    assert renewed.results.confirmed_stream_key == FOUND_KEY           # прежнее подтверждение — до нового


def test_record_needs_a_ready_channel(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path)
    item.channel_object = Channel(config=item.channel, token_file=Path("t.json"), status=ChannelStatus.FAILED)
    assert item.record_channel_id is None
    with pytest.raises(ValueError):
        item.to_record(MEMORY_NOW, SlotStage.ADMITTED)


def test_fixed_and_unfixed_fields_change_only_through_their_methods(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path, decision=Decision.UPDATE)
    item.mark_unfixed(ChangedField.THUMBNAIL)
    item.mark_fixed((ChangedField.TITLE,))
    item.mark_unfixed(ChangedField.TITLE)                 # уже исправлено — «не удалось» не ставится
    assert (item.fixed_fields, item.unfixed_fields) == ((ChangedField.TITLE,), (ChangedField.THUMBNAIL,))
    item.mark_fixed((ChangedField.THUMBNAIL, ChangedField.CATEGORY))   # порядок — как в ChangedField
    assert item.fixed_fields == (ChangedField.TITLE, ChangedField.CATEGORY, ChangedField.THUMBNAIL)
    assert item.unfixed_fields == ()


def test_recorded_thumbnail_overrides_the_placeholder_only_for_the_same_broadcast(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path)
    assert item.found is not None
    item.actual = replace(item.expected, has_own_thumbnail=False)
    assert not item.apply_recorded_thumbnail()            # записи нет — решает картинка
    item.remember_thumbnail(item.found.broadcast_id, MEMORY_NOW)
    item.record = item.to_record(MEMORY_NOW, SlotStage.PUBLISHED)
    assert (item.record.results.thumbnail_broadcast_id, item.record.results.thumbnail_set_at) == (
        "fbc", format_datetime_text(MEMORY_NOW.astimezone())
    )
    assert item.apply_recorded_thumbnail() and item.actual.has_own_thumbnail is True
    assert not item.apply_recorded_thumbnail()            # уже своя — переопределять нечего
    other: PlannedBroadcast = _memory_item(tmp_path)
    other.found = replace(other.found, broadcast_id="new")
    other.record = item.record
    other.actual = replace(other.expected, has_own_thumbnail=False)
    assert not other.apply_recorded_thumbnail() and other.actual.has_own_thumbnail is False


def test_thumbnail_fact_is_carried_while_the_broadcast_is_the_same(tmp_path: Path) -> None:
    item: PlannedBroadcast = _memory_item(tmp_path)
    item.remember_thumbnail("fbc", MEMORY_NOW)
    item.record = item.to_record(MEMORY_NOW, SlotStage.PUBLISHED)
    later: PlannedBroadcast = _memory_item(tmp_path)
    later.record = item.record
    assert later.to_record(MEMORY_NOW, SlotStage.PUBLISHED).results.thumbnail_broadcast_id == "fbc"
    renewed: PlannedBroadcast = _memory_item(tmp_path, found=False)
    renewed.record = item.record
    renewed.take_new_key(CREATED)                          # эфир создан заново — прежний факт не про него
    assert renewed.to_record(MEMORY_NOW, SlotStage.PUBLISHED).results.thumbnail_broadcast_id is None


def test_bootstrap_only_for_broadcasts_older_than_memory(tmp_path: Path) -> None:
    older: PlannedBroadcast = _memory_item(tmp_path)
    assert older.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)
    newer: PlannedBroadcast = _memory_item(tmp_path)
    assert newer.found is not None
    newer.found = replace(newer.found, published_utc=MEMORY_CREATED + timedelta(minutes=1))
    assert not newer.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED)
    unknown: PlannedBroadcast = _memory_item(tmp_path)
    assert unknown.found is not None
    unknown.found = replace(unknown.found, published_utc=None)
    assert not unknown.bootstrap_confirmation(MEMORY_NOW, MEMORY_CREATED) and _decided(unknown)
