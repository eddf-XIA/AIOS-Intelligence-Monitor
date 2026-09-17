#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Create a backup of the database and exported reports.

    python scripts/backup.py
    python scripts/backup.py --output D:\backups
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios.config import get_paths  # noqa: E402
from aios.services.backup import create_backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="", help="directory for the archive")
    parser.add_argument("--no-reports", action="store_true", help="database only")
    args = parser.parse_args()

    paths = get_paths()
    if not paths.db_path.exists():
        print(f"  No database at {paths.db_path} - nothing to back up.")
        return 1

    archive = create_backup(
        destination_dir=Path(args.output) if args.output else None,
        include_reports=not args.no_reports,
    )
    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"  Backup written: {archive}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
