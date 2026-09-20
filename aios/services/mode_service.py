"""Application mode: 简易版 (simple) or 本地专业版 (professional).

The mode changes *workflow and presentation only*. Both modes read and write the
same database, the same events, the same reports - see the architecture note in
the README. Nothing here filters data; it only decides which shell and which
home page the user gets.

Stored in the settings table so the preference survives a restart, which is the
whole point: a normal user should never have to re-choose 简易版 after a reboot.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from . import settings_service

#: Internal values. Never shown to the user - the labels below are.
MODE_SIMPLE = "simple"
MODE_PROFESSIONAL = "professional"

MODES = (MODE_SIMPLE, MODE_PROFESSIONAL)

#: Chinese labels for the segmented control.
MODE_LABELS = {
    MODE_SIMPLE: "简易版",
    MODE_PROFESSIONAL: "本地专业版",
}

#: Settings key holding the preference.
SETTING_KEY = "app_mode"

#: 简易版 is the recommended default: a first-time user should land on the
#: one-page research workflow, not on a monitoring administration console.
DEFAULT_MODE = MODE_SIMPLE


def normalize(value: Optional[str]) -> str:
    """Coerce anything to a valid mode, falling back to the default."""
    mode = (value or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def get_mode(session: Session) -> str:
    """The user's stored mode preference."""
    return normalize(settings_service.get_str(session, SETTING_KEY, DEFAULT_MODE))


def set_mode(session: Session, value: str) -> str:
    """Persist the mode preference. Returns the normalised value actually stored.

    Writes nothing except this one setting: switching modes must never touch
    monitoring configuration, events, reports or provider credentials.
    """
    mode = normalize(value)
    settings_service.set_value(session, SETTING_KEY, mode)
    return mode


def is_simple(session: Session) -> bool:
    return get_mode(session) == MODE_SIMPLE


def label(mode: str) -> str:
    return MODE_LABELS.get(normalize(mode), MODE_LABELS[DEFAULT_MODE])


def other(mode: str) -> str:
    """The mode the switch would move the user to."""
    return MODE_PROFESSIONAL if normalize(mode) == MODE_SIMPLE else MODE_SIMPLE
