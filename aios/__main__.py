"""Allow ``python -m aios`` as an alternative to ``python app.py``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
