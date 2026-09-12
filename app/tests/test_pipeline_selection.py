from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from app.config.loader import PlanerConfig
from app.core.dates import build_slot_id, format_date, format_time
from app.package.model import Package, Slot
from app.pipeline.selection import Selection, SkipReason, build_planned
from app.platforms.fake import FakePlatform
from app.state.registry import Registry
from app.tests.conftest import build_form_spec, build_package_object

KYIV_WINTER: timezone = timezone(timedelta(hours=2))
ConfigFactory = Callable[..., PlanerConfig]


def _slot(now: datetime, *, minutes: int, language: str) -> Slot:
    start: datetime = (now + timedelta(minutes=minutes)).astimezone(KYIV_WINTER)
    date_text: str = format_date(start.date())
    time_text: str = format_time(start.time())
    return Slot(
        slot_id=build_slot_id(date_text, time_text, language),
        date=date_text,
        time=time_text,
        start=start,
        language=language,
        title="Эфир",
        description="",
        previews=(),
        sources=(),
        form=build_form_spec(),
    )


_PACKAGE: Package = build_package_object()


def _slot_map(*slots: Slot) -> dict[str, Slot]:
    return {slot.slot_id: slot for slot in slots}


def _select(slot_map: dict[str, Slot], config: PlanerConfig, now: datetime) -> Selection:
    """Реальный production-путь: пакет-источник один на все слоты, журнал пуст."""
    sources: dict[str, Package] = {slot_id: _PACKAGE for slot_id in slot_map}
    return build_planned(slot_map, sources, config, FakePlatform().limits, Registry(), now)


def test_too_late_by_min_lead_minutes(now: datetime, make_config: ConfigFactory) -> None:
    soon: Slot = _slot(now, minutes=30, language="uk")
    boundary: Slot = _slot(now, minutes=60, language="uk")
    selection: Selection = _select(_slot_map(soon, boundary), make_config(min_lead_minutes=60), now)
    assert [(item.slot.slot_id, item.reason) for item in selection.skipped] == [(soon.slot_id, SkipReason.TOO_LATE)]
    assert [item.slot.slot_id for item in selection.planned] == [boundary.slot_id]


def test_language_without_channel_is_skipped(now: datetime, make_config: ConfigFactory) -> None:
    slot: Slot = _slot(now, minutes=180, language="hu")
    selection: Selection = _select(_slot_map(slot), make_config(), now)
    assert [(item.slot.slot_id, item.reason) for item in selection.skipped] == [(slot.slot_id, SkipReason.NO_CHANNEL)]
    assert selection.planned == ()


def test_two_channels_for_one_language_give_two_objects(now: datetime, make_config: ConfigFactory) -> None:
    slot: Slot = _slot(now, minutes=180, language="uk")
    config: PlanerConfig = make_config([("yt_b", ["uk"]), ("yt_a", ["uk"])])
    selection: Selection = _select(_slot_map(slot), config, now)
    assert [item.channel.id for item in selection.planned] == ["yt_a", "yt_b"]


def test_objects_are_ordered_by_start_language_and_channel(now: datetime, make_config: ConfigFactory) -> None:
    later_en: Slot = _slot(now, minutes=300, language="en")
    early_uk: Slot = _slot(now, minutes=120, language="uk")
    early_ru: Slot = _slot(now, minutes=120, language="ru")
    selection: Selection = _select(_slot_map(later_en, early_uk, early_ru), make_config(), now)
    assert [(item.slot.slot_id, item.channel.id) for item in selection.planned] == [
        (early_ru.slot_id, "yt_ru"),
        (early_uk.slot_id, "yt_ua"),
        (later_en.slot_id, "yt_ru"),
    ]
