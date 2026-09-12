"""Построение объектов запланированных эфиров (ТЗ §7.2): слот → объекты по каналам.

Слоты уже слиты по slot_id в app/package/promo.py, и только после этого каждый
размножается по каналам своего языка. Сливать расширенные объекты нельзя: один слот
из двух пакетов дал бы два эфира на один канал.

Слот внутри min_lead_minutes не выбрасывается: у него есть канал, значит есть и объект,
просто с признаком too_late. Иначе его ключ пропал бы из keys.txt ровно перед эфиром.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from app.config.loader import ChannelConfig, PlanerConfig
from app.observability.logging_setup import get_logger
from app.package.model import Package, Slot, slot_order_key
from app.pipeline.plan import BroadcastSpec, Decision, PlannedBroadcast
from app.platforms.base import PlatformLimits
from app.state.registry import Registration, Registry

LOGGER = get_logger("selection")


class SkipReason(str, Enum):
    NO_CHANNEL = "no_channel"  # язык слота не входит ни в один channels[].languages


@dataclass(frozen=True)
class SkippedSlot:
    slot: Slot
    reason: SkipReason


@dataclass(frozen=True)
class Selection:
    planned: tuple[PlannedBroadcast, ...]   # по (дата, время, язык, id канала)
    skipped: tuple[SkippedSlot, ...]


def build_planned(
    slot_map: Mapping[str, Slot],
    slot_sources: Mapping[str, Package],
    config: PlanerConfig,
    limits: PlatformLimits,
    registry: Registry,
    now: datetime,
) -> Selection:
    """Два канала на один язык → два объекта (два эфира)."""
    lead: timedelta = timedelta(minutes=config.min_lead_minutes)
    planned: list[PlannedBroadcast] = []
    skipped: list[SkippedSlot] = []
    for slot in sorted(slot_map.values(), key=slot_order_key):
        channels: list[ChannelConfig] = [
            channel for channel in config.channels if slot.language in channel.languages
        ]
        if not channels:
            skipped.append(SkippedSlot(slot=slot, reason=SkipReason.NO_CHANNEL))
            continue
        is_too_late: bool = slot.start - now < lead
        planned.extend(
            _build_one(slot, slot_sources[slot.slot_id], channel, limits, registry, is_too_late)
            for channel in channels
        )
    planned.sort(key=lambda item: (*slot_order_key(item.slot), item.channel.id))
    LOGGER.info(
        "selection_done planned=%d too_late=%d skipped=%d",
        len(planned),
        sum(1 for item in planned if item.is_too_late),
        len(skipped),
    )
    return Selection(planned=tuple(planned), skipped=tuple(skipped))


def _build_one(
    slot: Slot,
    source_package: Package,
    channel: ChannelConfig,
    limits: PlatformLimits,
    registry: Registry,
    is_too_late: bool,
) -> PlannedBroadcast:
    planned: PlannedBroadcast = PlannedBroadcast(
        slot=slot,
        source_package=source_package,
        channel=channel,
        expected=BroadcastSpec.from_slot(slot, limits),
        is_too_late=is_too_late,
        decision=Decision.TOO_LATE if is_too_late else Decision.CREATE,
    )
    registration: Registration | None = registry.get(planned.key)
    if registration is not None:
        planned.apply_registration(registration)
    return planned
