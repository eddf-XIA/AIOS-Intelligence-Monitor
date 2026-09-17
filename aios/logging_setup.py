"""Logging configuration.

Logs go to the console and to a rotating file under ``data/logs``. A filter
scrubs anything key-shaped on the way out, so a stray exception string can never
put a provider API key on disk.
"""

from __future__ import annotations

import logging
import logging.handlers
from typing import Optional

from .config import get_paths
from .services.keyring_service import redact

_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """Removes API-key-shaped substrings from every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: redact(v) if isinstance(v, str) else v
                        for k, v in record.args.items()
                    }
                else:
                    record.args = tuple(
                        redact(a) if isinstance(a, str) else a for a in record.args
                    )
        except Exception:  # pragma: no cover - never break logging
            pass
        return True


def configure_logging(level: int = logging.INFO, log_file: Optional[str] = None) -> None:
    """Install console + rotating file handlers exactly once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    paths = get_paths().ensure()
    target = log_file or str(paths.logs_dir / "aios.log")

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    redactor = RedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)

    file_handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [console, file_handler]

    # These are chatty and add nothing for a single-user local app.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("alembic").setLevel(logging.WARNING)

    _CONFIGURED = True
