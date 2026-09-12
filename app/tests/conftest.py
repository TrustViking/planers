"""Общие фикстуры: фиксированное «сейчас», папки планера в tmp_path, фабрика пакетов."""
from __future__ import annotations

import copy
import json
import os
import random
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config.loader import ChannelConfig, Platform, PlanerConfig, Privacy
from app.core.dates import build_slot_id, format_date, format_time, parse_date, parse_time
from app.form.base import FormSendResult
from app.package.model import FormSpec, Package, Slot
from app.pipeline.plan import BroadcastSpec, PlannedBroadcast
from app.platforms.base import PlatformLimits
from app.paths import PlanerPaths, build_paths, ensure_dirs
from app.platforms.fake import FakePlatform
from app.state.registry import Registration

KYIV_WINTER: timezone = timezone(timedelta(hours=2))
FIXED_NOW: datetime = datetime(2027, 3, 16, 12, 0, tzinfo=KYIV_WINTER)
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FORM_SPEC: dict[str, Any] = {
    "url": "https://forms.gle/UjVo2gftZdHsEdpZ7",
    "fields": {
        "language": "Язык стрима ( Language of stream)",
        "account_name": "Название канала ( Channel name)",
        "date": "Время стрима ( Stream time )",
        "platform": "Платформа (Platform)",
        "stream_key": "You Tube Stream Key",
        "stream_url": "Stream-URL (YT)",
        "time": None,
        "broadcast_url": None,
        "slot_id": None,
    },
    "values": {
        "language": {"uk": "Украинский ( Ukranian)", "ru": "Русский ( Russian)", "en": "Английский ( English)"},
        "platform": {"youtube": "You Tube", "facebook": "Facebook", "rumble": "Rumble"},
    },
    "date_format": "%d.%m.%Y",
}


def build_slot_spec(
    date_text: str = "17-03-2027",
    time_text: str = "19:00",
    language: str = "uk",
    *,
    previews: int = 1,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Слот манифеста §5.1; start — те же дата и время по Киеву (зимнее смещение +02:00)."""
    slot_id: str = build_slot_id(date_text, time_text, language)
    start: datetime = datetime.combine(parse_date(date_text), parse_time(time_text), tzinfo=KYIV_WINTER)
    return {
        "slot_id": slot_id,
        "broadcaster_slot_key": slot_id,
        "date": date_text,
        "time": time_text,
        "start": start.isoformat(),
        "language": language,
        "title": title or f"Эфир {slot_id}",
        "description": description or "Описание эфира",
        "previews": [f"previews/{slot_id}_{index}.jpg" for index in range(1, previews + 1)],
        "sources": ["https://www.youtube.com/watch?v=abcdefghijk"],
    }


def build_config(
    channels: Iterable[tuple[str, list[str]]] = (("yt_ua", ["uk"]), ("yt_ru", ["ru", "en"])),
    *,
    min_lead_minutes: int = 60,
    keep_days: int = 30,
) -> PlanerConfig:
    return PlanerConfig(
        owner="Тест",
        min_lead_minutes=min_lead_minutes,
        keep_days=keep_days,
        channels=tuple(
            ChannelConfig(
                id=channel_id,
                platform=Platform.YOUTUBE,
                account_name=f"Account {channel_id}",
                languages=tuple(languages),
                privacy=Privacy.PUBLIC,
                auto_start=True,
                set_thumbnail=True,
            )
            for channel_id, languages in channels
        ),
    )


def build_form_spec(url: str = FORM_SPEC["url"]) -> FormSpec:
    """FormSpec из того же образца, что кладётся в манифест (для слотов без пакета)."""
    return FormSpec(
        url=url,
        fields=dict(FORM_SPEC["fields"]),
        values={key: dict(options) for key, options in FORM_SPEC["values"].items()},
        date_format=FORM_SPEC["date_format"],
    )


def build_slot(
    start: datetime,
    language: str,
    *,
    title: str = "Эфир",
    description: str = "Описание эфира",
    form: FormSpec | None = None,
) -> Slot:
    """Слот в памяти (для сверки без пакета); дата и время — по Киеву (+02:00)."""
    local: datetime = start.astimezone(KYIV_WINTER)
    date_text: str = format_date(local.date())
    time_text: str = format_time(local.time())
    return Slot(
        slot_id=build_slot_id(date_text, time_text, language),
        date=date_text,
        time=time_text,
        start=local,
        language=language,
        title=title,
        description=description,
        previews=(),
        sources=(),
        form=form or build_form_spec(),
    )


def build_package_object(package_id: str = "pkg-test", path: Path | None = None) -> Package:
    """Package без архива: нужен объектам как источник package_id и превью."""
    return Package(
        path=path or Path("plan.bcast"),
        package_id=package_id,
        generated_at=datetime(2026, 9, 13, 10, 15),
        generator={"project": "pipeline", "version": "1.0.0"},
        timezone="Europe/Kyiv",
        period_from="17-03-2027",
        period_to="18-03-2027",
        form=build_form_spec(),
        slots=(),
    )


def build_planned(
    slot: Slot,
    channel: ChannelConfig,
    *,
    limits: PlatformLimits | None = None,
    package: Package | None = None,
) -> PlannedBroadcast:
    """Объект так же, как его строит production-путь (app/pipeline/selection.py)."""
    platform_limits: PlatformLimits = limits or FakePlatform().limits
    return PlannedBroadcast(
        slot=slot,
        source_package=package or build_package_object(),
        channel=channel,
        expected=BroadcastSpec.from_slot(slot, platform_limits),
    )


@dataclass(frozen=True)
class FormCall:
    slot_id: str
    channel_id: str
    stream_key: str | None
    form_url: str


class FakeFormSender:
    """Отправитель формы для тестов: подтверждает (или возвращает ошибку) и записывает вызовы."""

    def __init__(self, *, confirmed: bool = True, error: str | None = None) -> None:
        self.confirmed: bool = confirmed
        self.error: str | None = error
        self.calls: list[FormCall] = []

    def send(self, planned: PlannedBroadcast) -> FormSendResult:
        self.calls.append(FormCall(planned.slot_id, planned.channel.id, planned.stream_key, planned.form.url))
        return FormSendResult(confirmed=self.confirmed, error=self.error)


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def rng() -> random.Random:
    return random.Random(0)


@pytest.fixture
def fake_platform() -> FakePlatform:
    return FakePlatform()


@pytest.fixture
def form_sender() -> FakeFormSender:
    return FakeFormSender()


@pytest.fixture
def make_slot_object() -> Callable[..., Slot]:
    return build_slot


@pytest.fixture
def repo_config_example() -> Path:
    return REPO_ROOT / "config" / "planer.example.yaml"


@pytest.fixture
def repo_channels_example() -> Path:
    return REPO_ROOT / "config" / "channels.example.yaml"


@pytest.fixture
def planer_paths(tmp_path: Path) -> PlanerPaths:
    paths: PlanerPaths = build_paths(tmp_path / "planer")
    ensure_dirs(paths)
    return paths


@pytest.fixture
def make_slot() -> Callable[..., dict[str, Any]]:
    return build_slot_spec


@pytest.fixture
def make_config() -> Callable[..., PlanerConfig]:
    return build_config


@pytest.fixture
def make_package(tmp_path: Path) -> Callable[..., Path]:
    """Единственный способ собрать .bcast в тестах: manifest.json + previews/*.jpg по 1 байту."""

    def _make(
        directory: Path | None = None,
        *,
        generated_at: str = "13-09-2026 10:15",
        slots: list[dict[str, Any]] | None = None,
        form: dict[str, Any] | None = None,
        file_name: str | None = None,
        schema_version: int = 1,
        manifest_edit: Callable[[dict[str, Any]], object] | None = None,
        include_manifest: bool = True,
        omit_previews: Iterable[str] = (),
    ) -> Path:
        target_dir: Path = directory or (tmp_path / "packages")
        target_dir.mkdir(parents=True, exist_ok=True)
        slot_list: list[dict[str, Any]] = slots if slots is not None else [build_slot_spec()]
        manifest: dict[str, Any] = _manifest(generated_at, slot_list, form, schema_version)
        if manifest_edit is not None:
            manifest_edit(manifest)
        path: Path = target_dir / (file_name or f"plan_gen{generated_at.replace(' ', '-').replace(':', '')}.bcast")
        _write_archive(path, manifest if include_manifest else None, slot_list, set(omit_previews))
        # Пакет «пришёл сейчас»: чистка старья считает возраст от FIXED_NOW, а не от часов машины.
        os.utime(path, (FIXED_NOW.timestamp(), FIXED_NOW.timestamp()))
        return path

    return _make


def _manifest(
    generated_at: str,
    slots: list[dict[str, Any]],
    form: dict[str, Any] | None,
    schema_version: int,
) -> dict[str, Any]:
    dates: list[str] = sorted((slot["date"] for slot in slots), key=parse_date) or [generated_at.split(" ")[0]]
    return {
        "schema_version": schema_version,
        "package_id": f"pkg-{generated_at}",
        "generated_at": generated_at,
        "generator": {"project": "pipeline", "version": "1.0.0", "run_id": "run"},
        "timezone": "Europe/Kyiv",
        "period": {"from": dates[0], "to": dates[-1]},
        "form": copy.deepcopy(form or FORM_SPEC),
        "slots": copy.deepcopy(slots),
    }


def _write_archive(
    path: Path,
    manifest: dict[str, Any] | None,
    slots: list[dict[str, Any]],
    omit_previews: set[str],
) -> None:
    written: set[str] = set()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if manifest is not None:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
        for slot in slots:
            for preview in slot["previews"]:
                if preview in omit_previews or preview in written:
                    continue
                archive.writestr(preview, b"x")
                written.add(preview)
