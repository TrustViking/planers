"""Отбор слотов под каналы (ТЗ §7.2): слот → пары (слот, канал) или пропуск с причиной."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from app.config.loader import ChannelConfig, PlanerConfig
from app.observability.logging_setup import get_logger
from app.package.model import Slot, slot_order_key

LOGGER = get_logger("selection")


class SkipReason(str, Enum):
    TOO_LATE = "too_late"      # до старта меньше min_lead_minutes
    NO_CHANNEL = "no_channel"  # язык слота не входит ни в один channels[].languages


@dataclass(frozen=True)
class SlotChannelPair:
    slot: Slot
    channel: ChannelConfig


@dataclass(frozen=True)
class SkippedSlot:
    slot: Slot
    reason: SkipReason


@dataclass(frozen=True)
class Selection:
    pairs: tuple[SlotChannelPair, ...]   # по (дата, время, язык, id канала)
    skipped: tuple[SkippedSlot, ...]


def select_pairs(slot_map: dict[str, Slot], config: PlanerConfig, now: datetime) -> Selection:
    """Два канала на один язык → две пары (два эфира)."""
    lead: timedelta = timedelta(minutes=config.min_lead_minutes)
    pairs: list[SlotChannelPair] = []
    skipped: list[SkippedSlot] = []
    for slot in sorted(slot_map.values(), key=slot_order_key):
        if slot.start - now < lead:
            skipped.append(SkippedSlot(slot=slot, reason=SkipReason.TOO_LATE))
            continue
        channels: list[ChannelConfig] = [
            channel for channel in config.channels if slot.language in channel.languages
        ]
        if not channels:
            skipped.append(SkippedSlot(slot=slot, reason=SkipReason.NO_CHANNEL))
            continue
        pairs.extend(SlotChannelPair(slot=slot, channel=channel) for channel in channels)
    pairs.sort(key=lambda pair: (*slot_order_key(pair.slot), pair.channel.id))
    LOGGER.info("selection_done pairs=%d skipped=%d", len(pairs), len(skipped))
    return Selection(pairs=tuple(pairs), skipped=tuple(skipped))
