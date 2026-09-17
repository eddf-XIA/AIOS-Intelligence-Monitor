#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import a historical report produced by the original aios_daily.py.

    python scripts/import_report.py AIOS监测日报-2026-09-15.json
    python scripts/import_report.py *.json --overwrite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aios.config import get_paths  # noqa: E402
from aios.database import init_db, session_scope  # noqa: E402
from aios.services.importer import import_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", help="legacy audit JSON file(s)")
    parser.add_argument(
        "--overwrite", action="store_true", help="replace a report that already exists"
    )
    args = parser.parse_args()

    init_db(get_paths())

    failures = 0
    for pattern in args.files:
        matches = sorted(Path().glob(pattern)) if any(c in pattern for c in "*?[") else [Path(pattern)]
        if not matches:
            print(f"  no match: {pattern}")
            failures += 1
            continue
        for path in matches:
            with session_scope() as session:
                result = import_file(session, path, overwrite=args.overwrite)
            status = "OK " if result.ok else "ERR"
            print(f"  [{status}] {path.name}: {result.summary()}")
            if not result.ok:
                failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
