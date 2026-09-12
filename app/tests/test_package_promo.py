from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.package.promo import PromoScan, scan_promo
from app.package.model import PackageErrorReason
from app.paths import PlanerPaths

PackageFactory = Callable[..., Path]
SlotFactory = Callable[..., dict[str, Any]]


def test_newer_package_wins_for_the_same_slot(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    now: datetime,
) -> None:
    # имена файлов нарочно в обратном порядке: решает generated_at, а не имя
    make_package(planer_paths.promo_dir, generated_at="13-09-2026 10:15", file_name="b_old.bcast", slots=[make_slot(title="Старое")])
    make_package(planer_paths.promo_dir, generated_at="14-09-2026 09:00", file_name="a_new.bcast", slots=[make_slot(title="Новое")])
    scan: PromoScan = scan_promo(planer_paths, now)
    assert [item.package.path.name for item in scan.packages] == ["b_old.bcast", "a_new.bcast"]
    assert list(scan.slot_map) == ["17-03-2027_1900_uk"]
    assert scan.slot_map["17-03-2027_1900_uk"].title == "Новое"


def test_past_slots_leave_the_map(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    now: datetime,
) -> None:
    make_package(planer_paths.promo_dir, slots=[make_slot("15-03-2027"), make_slot("17-03-2027")])
    scan: PromoScan = scan_promo(planer_paths, now)
    assert list(scan.slot_map) == ["17-03-2027_1900_uk"]
    assert [slot.slot_id for slot in scan.past_slots] == ["15-03-2027_1900_uk"]
    [item] = scan.packages
    assert (item.slots_total, item.slots_past, item.slots_active) == (2, 1, 1)
    assert scan.all_past_packages == ()


def test_package_with_only_past_slots_is_all_past(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    now: datetime,
) -> None:
    path: Path = make_package(planer_paths.promo_dir, slots=[make_slot("14-03-2027"), make_slot("15-03-2027")])
    scan: PromoScan = scan_promo(planer_paths, now)
    assert [package.path for package in scan.all_past_packages] == [path]
    assert scan.packages[0].slots_active == 0
    assert scan.slot_map == {}


def test_damaged_file_is_a_problem_and_stays_in_place(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    now: datetime,
) -> None:
    broken: Path = planer_paths.promo_dir / "broken.bcast"
    broken.write_bytes(b"not a zip")
    make_package(planer_paths.promo_dir)
    scan: PromoScan = scan_promo(planer_paths, now)
    [problem] = scan.problems
    assert (problem.file, problem.reason) == (broken, PackageErrorReason.NOT_ZIP)
    assert broken.exists()
    assert len(scan.packages) == 1


def test_subdirectories_are_not_scanned(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    now: datetime,
) -> None:
    """Пакеты читаются одним списком из корня promo; вложенные папки — не наше дело."""
    make_package(planer_paths.promo_dir / "old")
    assert scan_promo(planer_paths, now).is_empty


def test_slot_sources_point_to_the_winning_package(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    now: datetime,
) -> None:
    old: Path = make_package(
        planer_paths.promo_dir,
        generated_at="13-09-2026 10:15",
        file_name="old.bcast",
        slots=[make_slot("17-03-2027"), make_slot("18-03-2027")],
    )
    new: Path = make_package(planer_paths.promo_dir, generated_at="14-09-2026 09:00", file_name="new.bcast", slots=[make_slot("17-03-2027")])
    scan: PromoScan = scan_promo(planer_paths, now)
    assert scan.slot_sources["17-03-2027_1900_uk"].path == new
    assert scan.slot_sources["18-03-2027_1900_uk"].path == old
