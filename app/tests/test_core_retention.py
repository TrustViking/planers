from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from app.core.retention import cleanup_expired
from app.paths import PlanerPaths

KEEP_DAYS: int = 30


def _aged_file(path: Path, now: datetime, days: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp: float = (now - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_cleanup_removes_only_expired_files(planer_paths: PlanerPaths, now: datetime) -> None:
    old_package: Path = _aged_file(planer_paths.bcast_dir / "old.bcast", now, days=31)
    fresh_package: Path = _aged_file(planer_paths.bcast_dir / "fresh.bcast", now, days=29)
    old_log: Path = _aged_file(planer_paths.logs_dir / "01-01-2027_120000_planer.log", now, days=40)
    old_report: Path = _aged_file(planer_paths.logs_dir / "01-01-2027_120000_report.md", now, days=40)
    fresh_log: Path = _aged_file(planer_paths.logs_dir / "15-03-2027_120000_planer.log", now, days=1)

    removed: list[Path] = cleanup_expired(planer_paths, KEEP_DAYS, now)

    assert sorted(path.name for path in removed) == [
        "01-01-2027_120000_planer.log",
        "01-01-2027_120000_report.md",
        "old.bcast",
    ]
    assert not old_package.exists() and not old_log.exists() and not old_report.exists()
    assert fresh_package.exists() and fresh_log.exists()


def test_cleanup_touches_only_bcast_and_logs(planer_paths: PlanerPaths, now: datetime) -> None:
    """keystreams\\, app\\state\\, config\\ и вложенные папки — не её дело."""
    keys: Path = _aged_file(planer_paths.keys_file, now, days=90)
    bindings: Path = _aged_file(planer_paths.bindings_file, now, days=90)
    nested: Path = _aged_file(planer_paths.bcast_dir / "old" / "nested.bcast", now, days=90)

    assert cleanup_expired(planer_paths, KEEP_DAYS, now) == []
    assert keys.exists() and bindings.exists() and nested.exists()
