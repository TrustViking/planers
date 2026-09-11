"""inbox\\ → единая карта слотов (ТЗ §7.1); перенос в done\\ / archive\\ и чистка — отдельно."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from app.observability.logging_setup import get_logger
from app.package.model import Package, PackageError, PackageErrorReason, Slot, slot_order_key
from app.package.reader import read_package
from app.paths import PlanerPaths

LOGGER = get_logger("inbox")
PACKAGE_GLOB: Final[str] = "*.bcast"


@dataclass(frozen=True)
class PackageProblem:
    file: Path
    reason: PackageErrorReason
    detail: str


@dataclass(frozen=True)
class AcceptedPackage:
    package: Package
    slots_total: int
    slots_past: int
    slots_active: int


@dataclass(frozen=True)
class InboxScan:
    packages: tuple[AcceptedPackage, ...]       # по generated_at, от старого к новому
    problems: tuple[PackageProblem, ...]
    slot_map: dict[str, Slot]                   # будущие слоты всех пакетов, новее побеждает
    past_slots: tuple[Slot, ...]                # из той же карты, start <= now
    all_past_packages: tuple[Package, ...]      # у пакета нет ни одного будущего слота
    slot_sources: dict[str, Package]            # slot_id → пакет-победитель (превью, package_id)

    @property
    def is_empty(self) -> bool:
        return not self.packages and not self.problems


def list_package_files(paths: PlanerPaths) -> list[Path]:
    """*.bcast только из корня inbox\\ — done\\ и archive\\ не сканируются."""
    return sorted(path for path in paths.inbox_dir.glob(PACKAGE_GLOB) if path.is_file())


def scan_inbox(paths: PlanerPaths, now: datetime) -> InboxScan:
    """Читает все пакеты, сливает слоты по slot_id (новее побеждает), отделяет прошлое."""
    packages, problems = _read_packages(list_package_files(paths))
    packages.sort(key=lambda package: (package.generated_at, package.path.name))
    merged, slot_sources = _merge_slots(packages)
    stats: tuple[AcceptedPackage, ...] = tuple(_package_stats(package, now) for package in packages)
    scan: InboxScan = InboxScan(
        packages=stats,
        problems=tuple(problems),
        slot_map={slot_id: slot for slot_id, slot in merged.items() if slot.start > now},
        past_slots=tuple(sorted((slot for slot in merged.values() if slot.start <= now), key=slot_order_key)),
        all_past_packages=tuple(item.package for item in stats if item.slots_active == 0),
        slot_sources=slot_sources,
    )
    LOGGER.info(
        "inbox_scanned packages=%d problems=%d active_slots=%d past_slots=%d all_past_packages=%d",
        len(scan.packages),
        len(scan.problems),
        len(scan.slot_map),
        len(scan.past_slots),
        len(scan.all_past_packages),
    )
    return scan


def _read_packages(files: list[Path]) -> tuple[list[Package], list[PackageProblem]]:
    packages: list[Package] = []
    problems: list[PackageProblem] = []
    for file_path in files:
        try:
            packages.append(read_package(file_path))
        except PackageError as error:
            LOGGER.warning(
                "package_rejected file=%s reason=%s detail=%s",
                file_path.name,
                error.reason.value,
                error.detail,
            )
            problems.append(PackageProblem(file=file_path, reason=error.reason, detail=error.detail))
    return packages, problems


def _merge_slots(packages: list[Package]) -> tuple[dict[str, Slot], dict[str, Package]]:
    """packages — от старого к новому; тот же slot_id из более нового пакета заменяет старый.

    Возвращает карту слотов и пакет-победитель каждого slot_id.
    """
    merged: dict[str, Slot] = {}
    source_by_slot: dict[str, Package] = {}
    for package in packages:
        for slot in package.slots:
            previous: Package | None = source_by_slot.get(slot.slot_id)
            if previous is not None:
                LOGGER.info(
                    "slot_superseded slot_id=%s old_package=%s new_package=%s",
                    slot.slot_id,
                    previous.path.name,
                    package.path.name,
                )
            merged[slot.slot_id] = slot
            source_by_slot[slot.slot_id] = package
    return merged, source_by_slot


def _package_stats(package: Package, now: datetime) -> AcceptedPackage:
    slots_past: int = sum(1 for slot in package.slots if slot.start <= now)
    return AcceptedPackage(
        package=package,
        slots_total=len(package.slots),
        slots_past=slots_past,
        slots_active=len(package.slots) - slots_past,
    )


def archive_package(paths: PlanerPaths, package: Package, moved_at: datetime | None = None) -> Path:
    """В inbox\\archive\\ — все слоты пакета в прошлом."""
    return _move_package(package.path, paths.inbox_archive_dir, moved_at)


def finish_package(paths: PlanerPaths, package: Package, moved_at: datetime | None = None) -> Path:
    """В inbox\\done\\ — все слоты пакета под мои языки в терминальном состоянии."""
    return _move_package(package.path, paths.inbox_done_dir, moved_at)


def _move_package(source: Path, target_dir: Path, moved_at: datetime | None) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target: Path = target_dir / source.name
    os.replace(source, target)
    # os.replace сохраняет mtime, а cleanup_expired считает возраст по mtime:
    # срок inbox_keep_days должен идти от переноса, а не от скачивания пакета.
    # moved_at — «сейчас» запуска (тот же источник, что у cleanup_expired); None — часы системы.
    os.utime(target, None if moved_at is None else (moved_at.timestamp(), moved_at.timestamp()))
    LOGGER.info("package_moved file=%s to=%s", source.name, target_dir)
    return target


def cleanup_expired(paths: PlanerPaths, keep_days: int, now: datetime) -> list[Path]:
    """Удаляет из done\\ и archive\\ файлы, изменённые раньше, чем keep_days дней назад."""
    border: datetime = now - timedelta(days=keep_days)
    removed: list[Path] = []
    for directory in (paths.inbox_done_dir, paths.inbox_archive_dir):
        for file_path in _files_older_than(directory, border):
            try:
                file_path.unlink()
            except OSError as error:
                LOGGER.warning("inbox_cleanup_failed file=%s reason=%s", file_path, error)
                continue
            LOGGER.info("inbox_cleanup_removed file=%s", file_path)
            removed.append(file_path)
    return removed


def _files_older_than(directory: Path, border: datetime) -> list[Path]:
    if not directory.is_dir():
        return []
    return [
        file_path
        for file_path in sorted(directory.iterdir())
        if file_path.is_file()
        and datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc) < border
    ]
