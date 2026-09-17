"""Optional Windows auto-start.

The division of labour is deliberate and must stay this way:

* **Windows** starts AIOS once, at user logon.
* **APScheduler**, inside AIOS, decides when the daily monitoring run happens.

Windows is never used to run monitoring directly - that would bypass the
scheduler settings, the concurrency guard and the run history.

Two mechanisms, chosen automatically:

``task_scheduler``
    A Task Scheduler entry with an ONLOGON trigger. Preferred when available,
    but Windows requires **administrator rights** to register a logon trigger
    (unlike DAILY/ONCE tasks, which a normal user may create).

``startup_folder``
    A shortcut in the per-user Startup folder. Needs no elevation at all, which
    is why it is the fallback: the spec asks for auto-start "without
    administrator privileges if avoidable".

Nothing secret is written by either mechanism: the command line carries only
the interpreter, the entry point and ``--no-browser``. Credentials continue to
come from the OS keyring after the process starts.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..config import DEFAULT_HOST, DEFAULT_PORT, PROJECT_ROOT

logger = logging.getLogger(__name__)

#: Stable name so enable/disable always target the same entry.
TASK_NAME = "AIOS Intelligence Monitor"
#: Stable stem for the Startup-folder entry.
SHORTCUT_STEM = "AIOS Intelligence Monitor"

MECHANISM_TASK = "task_scheduler"
MECHANISM_FOLDER = "startup_folder"

MECHANISM_LABELS = {
    MECHANISM_TASK: "任务计划程序（Task Scheduler）",
    MECHANISM_FOLDER: "启动文件夹（Startup）",
}

#: schtasks writes localised output; these are just for decoding its messages.
_ENCODINGS = ("utf-8", "gbk", "cp936", "latin-1")


@dataclass(frozen=True)
class StartupStatus:
    """What the Settings page shows for auto-start."""

    supported: bool
    enabled: bool
    task_name: str = TASK_NAME
    command: str = ""
    mechanism: str = ""
    detail: str = ""
    error: str = ""

    @property
    def mechanism_label(self) -> str:
        return MECHANISM_LABELS.get(self.mechanism, "")


def is_supported() -> bool:
    """Auto-start is Windows-only; other platforms see a disabled control."""
    return sys.platform == "win32"


def is_elevated() -> bool:
    """True when the process can register a logon-triggered scheduled task."""
    if not is_supported():
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - platform dependent
        return False


def _decode(raw: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a helper process and return ``(returncode, combined output)``."""
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            # Never surface a console window when called from the web UI.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return 1, f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 1, f"{args[0]} timed out"
    output = _decode(completed.stdout or b"") + _decode(completed.stderr or b"")
    return completed.returncode, output.strip()


# --- what gets launched -----------------------------------------------------

def python_executable() -> Path:
    """The interpreter to launch.

    Prefers ``pythonw.exe`` from the same installation so logon does not flash a
    console window - but only when it actually exists next to ``python.exe``,
    since reliability matters more than hiding the terminal.
    """
    current = Path(sys.executable)
    if sys.platform == "win32":
        windowless = current.with_name("pythonw.exe")
        if windowless.exists():
            return windowless
    return current


def entry_point() -> Path:
    """Absolute path to ``app.py`` for the project this code belongs to.

    Resolved at call time so moving the project directory only requires the
    user to disable and re-enable auto-start.
    """
    return (PROJECT_ROOT / "app.py").resolve()


def build_command() -> str:
    """The exact command Windows will run at logon.

    ``--no-browser`` matters twice: a browser tab opening on every logon would
    be obnoxious, and that flag also makes a duplicate start exit quietly when
    AIOS is already running.
    """
    return f'"{python_executable()}" "{entry_point()}" --no-browser'


# --- mechanism 1: Task Scheduler --------------------------------------------

def _task_exists() -> tuple[bool, str]:
    code, output = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    return code == 0, output


def _create_task() -> tuple[bool, str]:
    code, output = _run(
        [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", build_command(),
            "/SC", "ONLOGON",
            "/F",
        ]
    )
    return code == 0, output


def _delete_task() -> tuple[bool, str]:
    code, output = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    return code == 0, output


# --- mechanism 2: Startup folder --------------------------------------------

def startup_dir() -> Path:
    """The current user's Startup folder."""
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_dir() / f"{SHORTCUT_STEM}.lnk"


def batch_path() -> Path:
    return startup_dir() / f"{SHORTCUT_STEM}.cmd"


def _create_shortcut() -> tuple[bool, str]:
    """Write a .lnk via the WScript.Shell COM object, driven by PowerShell.

    A shortcut launching ``pythonw.exe`` opens no console at all. If PowerShell
    or COM is unavailable we fall back to a .cmd, which may flash a window for
    an instant but always works.
    """
    directory = startup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, str(exc)

    target = python_executable()
    arguments = f'"{entry_point()}" --no-browser'
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('"
        f"{shortcut_path()}');"
        f"$s.TargetPath = '{target}';"
        f"$s.Arguments = '{arguments}';"
        f"$s.WorkingDirectory = '{PROJECT_ROOT.resolve()}';"
        "$s.WindowStyle = 7;"
        f"$s.Description = 'Start {TASK_NAME} at logon';"
        "$s.Save()"
    )
    code, output = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    )
    if code == 0 and shortcut_path().exists():
        return True, "shortcut"

    # Fallback: a batch file. Less pretty, but dependable.
    try:
        batch_path().write_text(
            "@echo off\r\n"
            f'cd /d "{PROJECT_ROOT.resolve()}"\r\n'
            f'start "" "{target}" "{entry_point()}" --no-browser\r\n',
            encoding="utf-8",
        )
    except OSError as exc:
        return False, f"{output}\n{exc}".strip()
    return True, "batch"


def _remove_shortcut() -> bool:
    removed = False
    for path in (shortcut_path(), batch_path()):
        try:
            if path.exists():
                path.unlink()
                removed = True
        except OSError as exc:  # pragma: no cover - permissions
            logger.warning("Could not remove %s: %s", path.name, exc)
    return removed


def _shortcut_exists() -> bool:
    return shortcut_path().exists() or batch_path().exists()


# --- public API -------------------------------------------------------------

def get_status() -> StartupStatus:
    """Current auto-start state, safe to call on any platform."""
    if not is_supported():
        return StartupStatus(
            supported=False,
            enabled=False,
            detail="Windows 自动启动仅适用于 Windows。",
        )

    task_present, output = _task_exists()
    if task_present:
        return StartupStatus(
            supported=True, enabled=True, command=build_command(),
            mechanism=MECHANISM_TASK, detail=output,
        )
    if _shortcut_exists():
        return StartupStatus(
            supported=True, enabled=True, command=build_command(),
            mechanism=MECHANISM_FOLDER,
            detail=str(shortcut_path() if shortcut_path().exists() else batch_path()),
        )
    return StartupStatus(supported=True, enabled=False, command=build_command())


def enable() -> StartupStatus:
    """Make AIOS start at the next Windows logon.

    Uses Task Scheduler when the app is running elevated, because a logon
    trigger needs administrator rights; otherwise installs a Startup-folder
    entry, which achieves the same outcome with no privileges at all.
    """
    if not is_supported():
        return StartupStatus(
            supported=False, enabled=False, error="Windows 自动启动仅适用于 Windows。"
        )

    entry = entry_point()
    if not entry.exists():
        return StartupStatus(
            supported=True, enabled=False, error=f"找不到启动入口：{entry}"
        )

    command = build_command()
    task_error = ""

    if is_elevated():
        created, output = _create_task()
        if created:
            logger.info("Registered Windows logon task %r", TASK_NAME)
            # Avoid both mechanisms firing and racing each other.
            _remove_shortcut()
            return StartupStatus(
                supported=True, enabled=True, command=command,
                mechanism=MECHANISM_TASK, detail=output,
            )
        task_error = output
        logger.info("Logon task creation failed (%s); using the Startup folder", output)

    created, output = _create_shortcut()
    if not created:
        return StartupStatus(
            supported=True, enabled=False, command=command,
            error=(task_error + "\n" + output).strip() or "无法创建自动启动项。",
        )

    logger.info("Installed Startup-folder auto-start (%s)", output)
    return StartupStatus(
        supported=True, enabled=True, command=command,
        mechanism=MECHANISM_FOLDER, detail=str(shortcut_path()),
    )


def disable() -> StartupStatus:
    """Remove auto-start, whichever mechanism installed it."""
    if not is_supported():
        return StartupStatus(
            supported=False, enabled=False, error="Windows 自动启动仅适用于 Windows。"
        )

    removed_any = _remove_shortcut()
    task_present, _ = _task_exists()
    task_error = ""
    if task_present:
        deleted, output = _delete_task()
        if deleted:
            removed_any = True
        else:
            task_error = output

    still_on = get_status()
    if still_on.enabled:
        return StartupStatus(
            supported=True, enabled=True, command=build_command(),
            mechanism=still_on.mechanism,
            error=task_error or "删除自动启动项失败。",
        )

    return StartupStatus(
        supported=True, enabled=False, command=build_command(),
        detail="已移除" if removed_any else "任务本来就不存在。",
    )


def already_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> bool:
    """True when something already listens on the AIOS port.

    Used by the launcher so a logon start cannot stack a second server on top
    of a session the user opened by hand.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0
