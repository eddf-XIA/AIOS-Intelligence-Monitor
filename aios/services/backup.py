"""Database and report backups.

All history lives in one SQLite file, so a one-click backup matters. The DB is
copied through SQLite's own backup API rather than a file copy: that is safe
while the app is running and WAL data is pending, which a naive copy is not.
"""

from __future__ import annotations

import logging
import sqlite3
import zipfile
from pathlib import Path
from typing import Optional

from ..config import Paths, get_paths
from ..timeutil import local_now

logger = logging.getLogger(__name__)


def backup_filename(prefix: str = "aios-backup", suffix: str = "zip") -> str:
    return f"{prefix}-{local_now().strftime('%Y%m%d-%H%M%S')}.{suffix}"


def copy_database(destination: Path, paths: Optional[Paths] = None) -> Path:
    """Consistent snapshot of the database, safe to take while running."""
    paths = paths or get_paths()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(paths.db_path))
    try:
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return destination


def create_backup(
    destination_dir: Optional[Path] = None,
    include_reports: bool = True,
    paths: Optional[Paths] = None,
) -> Path:
    """Write a ZIP containing ``aios.db`` and, optionally, ``reports/``.

    Returns the path to the created archive.
    """
    paths = paths or get_paths()
    target_dir = Path(destination_dir) if destination_dir else paths.data_dir / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / backup_filename()

    staging = target_dir / f".staging-{archive_path.stem}.db"
    try:
        copy_database(staging, paths)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(staging, arcname="aios.db")
            if include_reports and paths.reports_dir.exists():
                for report_file in sorted(paths.reports_dir.rglob("*")):
                    if report_file.is_file():
                        bundle.write(
                            report_file,
                            arcname=f"reports/{report_file.relative_to(paths.reports_dir)}",
                        )
    finally:
        staging.unlink(missing_ok=True)

    logger.info("Created backup %s", archive_path)
    return archive_path


def list_backups(paths: Optional[Paths] = None) -> list[dict]:
    """Existing backup archives, newest first."""
    paths = paths or get_paths()
    directory = paths.data_dir / "backups"
    if not directory.exists():
        return []
    rows = []
    for item in directory.glob("aios-backup-*.zip"):
        stat = item.stat()
        rows.append(
            {
                "name": item.name,
                "path": str(item),
                "size_bytes": stat.st_size,
                "size_text": _human_size(stat.st_size),
                "modified": stat.st_mtime,
            }
        )
    rows.sort(key=lambda r: r["modified"], reverse=True)
    return rows


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
