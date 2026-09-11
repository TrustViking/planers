from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.package.inbox import (
    InboxScan,
    archive_package,
    cleanup_expired,
    finish_package,
    scan_inbox,
)
from app.package.model import PackageErrorReason
from app.package.reader import read_package
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
    make_package(planer_paths.inbox_dir, generated_at="13-09-2026 10:15", file_name="b_old.bcast", slots=[make_slot(title="Старое")])
    make_package(planer_paths.inbox_dir, generated_at="14-09-2026 09:00", file_name="a_new.bcast", slots=[make_slot(title="Новое")])
    scan: InboxScan = scan_inbox(planer_paths, now)
    assert [item.package.path.name for item in scan.packages] == ["b_old.bcast", "a_new.bcast"]
    assert list(scan.slot_map) == ["17-03-2027_1900_uk"]
    assert scan.slot_map["17-03-2027_1900_uk"].title == "Новое"


def test_past_slots_leave_the_map(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    make_slot: SlotFactory,
    now: datetime,
) -> None:
    make_package(planer_paths.inbox_dir, slots=[make_slot("15-03-2027"), make_slot("17-03-2027")])
    scan: InboxScan = scan_inbox(planer_paths, now)
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
    path: Path = make_package(planer_paths.inbox_dir, slots=[make_slot("14-03-2027"), make_slot("15-03-2027")])
    scan: InboxScan = scan_inbox(planer_paths, now)
    assert [package.path for package in scan.all_past_packages] == [path]
    assert scan.packages[0].slots_active == 0
    assert scan.slot_map == {}


def test_damaged_file_is_a_problem_and_stays_in_place(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    now: datetime,
) -> None:
    broken: Path = planer_paths.inbox_dir / "broken.bcast"
    broken.write_bytes(b"not a zip")
    make_package(planer_paths.inbox_dir)
    scan: InboxScan = scan_inbox(planer_paths, now)
    [problem] = scan.problems
    assert (problem.file, problem.reason) == (broken, PackageErrorReason.NOT_ZIP)
    assert broken.exists()
    assert len(scan.packages) == 1


def test_done_and_archive_are_not_scanned(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
    now: datetime,
) -> None:
    make_package(planer_paths.inbox_done_dir)
    make_package(planer_paths.inbox_archive_dir)
    assert scan_inbox(planer_paths, now).is_empty


def test_archive_and_finish_move_packages(
    planer_paths: PlanerPaths,
    make_package: PackageFactory,
) -> None:
    first: Path = make_package(planer_paths.inbox_dir, file_name="first.bcast")
    second: Path = make_package(planer_paths.inbox_dir, file_name="second.bcast")
    archived: Path = archive_package(planer_paths, read_package(first))
    finished: Path = finish_package(planer_paths, read_package(second))
    assert archived == planer_paths.inbox_archive_dir / "first.bcast"
    assert finished == planer_paths.inbox_done_dir / "second.bcast"
    assert archived.exists() and finished.exists()
    assert not first.exists() and not second.exists()


def test_cleanup_removes_only_expired_files(planer_paths: PlanerPaths, now: datetime) -> None:
    ages: dict[Path, int] = {
        planer_paths.inbox_done_dir / "old_done.bcast": 20,
        planer_paths.inbox_archive_dir / "old_archive.bcast": 15,
        planer_paths.inbox_archive_dir / "fresh_archive.bcast": 1,
        planer_paths.inbox_dir / "in_root.bcast": 30,
    }
    for path, days in ages.items():
        path.write_bytes(b"x")
        timestamp: float = (now - timedelta(days=days)).timestamp()
        os.utime(path, (timestamp, timestamp))
    removed: list[Path] = cleanup_expired(planer_paths, keep_days=14, now=now)
    assert sorted(path.name for path in removed) == ["old_archive.bcast", "old_done.bcast"]
    assert (planer_paths.inbox_archive_dir / "fresh_archive.bcast").exists()
    assert (planer_paths.inbox_dir / "in_root.bcast").exists()
