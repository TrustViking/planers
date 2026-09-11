from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from app.config.loader import PlanerConfig
from app.package.model import Slot
from app.pipeline.reconciler import (
    ChangedField,
    Decision,
    MarkerParts,
    ReconciledPair,
    Reconciler,
    Reconciliation,
    normalize_description,
    split_marker,
)
from app.pipeline.selection import SlotChannelPair
from app.platforms.base import PlatformError, UpcomingBroadcast
from app.platforms.fake import FakePlatform
from app.state.registry import FormStatus, Registration, Registry

ConfigFactory = Callable[..., PlanerConfig]
SlotFactory = Callable[..., Slot]


def _pairs(config: PlanerConfig, *slots: Slot) -> list[SlotChannelPair]:
    return [
        SlotChannelPair(slot=slot, channel=channel)
        for slot in slots
        for channel in config.channels
        if slot.language in channel.languages
    ]


def _registration(slot: Slot, channel_id: str, broadcast_id: str) -> Registration:
    return Registration(
        slot_id=slot.slot_id,
        channel_id=channel_id,
        account_name="Account",
        language=slot.language,
        date=slot.date,
        time=slot.time,
        broadcast_id=broadcast_id,
        broadcast_url=f"https://www.youtube.com/watch?v={broadcast_id}",
        stream_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="abcd-abcd-abcd-abcd-abcd",
        package_id="pkg",
        created_at=datetime(2027, 3, 1, 10, 0),
        form_status=FormStatus.SENT,
        form_sent_at=datetime(2027, 3, 1, 10, 0),
        previous_broadcast_ids=[],
        last_error=None,
    )


def _reconcile(
    platform: FakePlatform,
    config: PlanerConfig,
    *slots: Slot,
    registry: Registry | None = None,
    slot_ids: frozenset[str] | None = None,
) -> Reconciliation:
    ids: frozenset[str] = slot_ids if slot_ids is not None else frozenset(slot.slot_id for slot in slots)
    return Reconciler(platform).reconcile(_pairs(config, *slots), registry or Registry(), ids, config.channels)


def _seed_like(platform: FakePlatform, channel_id: str, slot: Slot, **overrides: object) -> UpcomingBroadcast:
    values: dict[str, object] = dict(
        start_utc=slot.start,
        title=slot.title,
        description=slot.description,
        marker=slot.slot_id,
    )
    values.update(overrides)
    return platform.seed_broadcast(channel_id, **values)  # type: ignore[arg-type]


def test_no_broadcast_means_create(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    [result] = _reconcile(fake_platform, make_config(), slot).pairs
    assert result.decision is Decision.CREATE


def test_marked_broadcast_with_same_texts_matches(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    seeded: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, stream_key="abcd-abcd-abcd-abcd-abcd")
    registry: Registry = Registry()
    registry.upsert(_registration(slot, "yt_ua", seeded.broadcast_id))
    [result] = _reconcile(fake_platform, make_config(), slot, registry=registry).pairs
    assert result.decision is Decision.MATCH
    assert result.broadcast == seeded
    assert result.rebind is False
    assert result.stream is not None and result.stream.stream_name == "abcd-abcd-abcd-abcd-abcd"


def test_changed_description_means_update(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot, description="Старое описание")
    [result] = _reconcile(fake_platform, make_config(), slot).pairs
    assert result.decision is Decision.UPDATE
    assert result.changed_fields == (ChangedField.DESCRIPTION,)


def test_outer_spaces_and_line_endings_do_not_count(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk", title="Эфир", description="Первый абзац\n\nВторой")
    _seed_like(fake_platform, "yt_ua", slot, title="  Эфир  ", description="Первый абзац  \r\n\r\nВторой\r\n")
    [result] = _reconcile(fake_platform, make_config(), slot).pairs
    assert result.decision is Decision.MATCH
    assert result.changed_fields == ()


def test_registered_broadcast_missing_on_platform_means_recreate(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    registry: Registry = Registry()
    registry.upsert(_registration(slot, "yt_ua", "deletedbc"))
    [result] = _reconcile(fake_platform, make_config(), slot, registry=registry).pairs
    assert result.decision is Decision.RECREATE


def test_two_languages_on_one_minute_each_find_their_marker(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    start: datetime = now + timedelta(days=1)
    ru: Slot = make_slot_object(start, "ru")
    en: Slot = make_slot_object(start, "en")
    en_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ru", en)
    ru_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ru", ru)
    results: tuple[ReconciledPair, ...] = _reconcile(fake_platform, make_config(), ru, en).pairs
    found: dict[str, str | None] = {
        result.pair.slot.slot_id: result.broadcast.broadcast_id if result.broadcast else None for result in results
    }
    assert found == {ru.slot_id: ru_broadcast.broadcast_id, en.slot_id: en_broadcast.broadcast_id}
    assert {result.decision for result in results} == {Decision.MATCH}
    assert len(fake_platform.stream_calls) == len(set(fake_platform.stream_calls))


def test_single_manual_broadcast_matches_with_rebind(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    manual: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", slot, marker=None)
    [result] = _reconcile(fake_platform, make_config(), slot).pairs
    assert result.decision is Decision.MATCH
    assert result.broadcast == manual
    assert result.rebind is True
    assert result.stream is None


def test_two_manual_broadcasts_are_ambiguous(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    _seed_like(fake_platform, "yt_ua", slot, marker=None)
    _seed_like(fake_platform, "yt_ua", slot, marker="Мой поток")
    [result] = _reconcile(fake_platform, make_config(), slot).pairs
    assert result.decision is Decision.AMBIGUOUS


def test_list_failure_is_isolated_to_its_channel(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    uk: Slot = make_slot_object(now + timedelta(days=1), "uk")
    ru: Slot = make_slot_object(now + timedelta(days=1), "ru")
    fake_platform.fail_list["yt_ua"] = PlatformError("quotaExceeded", "квота исчерпана")
    results: dict[str, ReconciledPair] = {
        result.pair.slot.slot_id: result for result in _reconcile(fake_platform, make_config(), uk, ru).pairs
    }
    assert results[uk.slot_id].decision is Decision.ERROR
    assert results[uk.slot_id].error is not None and results[uk.slot_id].error.code == "quotaExceeded"
    assert results[ru.slot_id].decision is Decision.CREATE


def test_list_upcoming_is_called_once_per_channel(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    start: datetime = now + timedelta(days=1)
    slots: list[Slot] = [make_slot_object(start, "uk"), make_slot_object(start, "ru"), make_slot_object(start, "en")]
    _reconcile(fake_platform, make_config(), *slots)
    assert fake_platform.list_calls == ["yt_ua", "yt_ru"]


def test_marker_of_slot_outside_the_map_is_an_orphan(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    moved: Slot = make_slot_object(now + timedelta(days=3), "uk")
    moved_broadcast: UpcomingBroadcast = _seed_like(fake_platform, "yt_ua", moved)
    reconciliation: Reconciliation = _reconcile(fake_platform, make_config(), slot)
    [orphan] = reconciliation.orphans
    assert (orphan.channel.id, orphan.broadcast, orphan.marker) == ("yt_ua", moved_broadcast, moved.slot_id)
    assert split_marker(orphan.marker) == MarkerParts(date="19-03-2027", time="12:00", language="uk")
    assert reconciliation.pairs[0].decision is Decision.CREATE


def test_manual_broadcast_at_another_time_is_ignored(fake_platform: FakePlatform, make_config: ConfigFactory, make_slot_object: SlotFactory, now: datetime) -> None:
    slot: Slot = make_slot_object(now + timedelta(days=1), "uk")
    fake_platform.seed_broadcast("yt_ua", now + timedelta(days=2), "Ручной", "", marker="Мой поток")
    reconciliation: Reconciliation = _reconcile(fake_platform, make_config(), slot)
    assert reconciliation.orphans == ()
    assert reconciliation.pairs[0].decision is Decision.CREATE


def test_normalize_description() -> None:
    assert normalize_description("  Текст  \r\n\r\nещё  \r\n") == "Текст\n\nещё"


def test_split_marker_rejects_non_markers() -> None:
    assert split_marker("Мой поток") is None
    assert split_marker("99-99-2027_1900_uk") is None
    assert split_marker("17-03-2027_1900_uk") == MarkerParts(date="17-03-2027", time="19:00", language="uk")
