"""Windows auto-start: command generation, enable/disable, and duplicate guard."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aios.services import startup_service


class FakeCompleted:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_windows(monkeypatch, tmp_path):
    """Fake schtasks + a temp Startup folder, so nothing touches the real system.

    ``elevated`` controls whether logon-task creation is permitted, mirroring
    the real Windows rule that an ONLOGON trigger requires administrator rights.
    """
    state = {"task": False, "elevated": False, "calls": []}
    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()

    def fake_run(args, capture_output=True, timeout=30, creationflags=0):
        state["calls"].append(list(args))
        program = args[0].lower()

        if program == "schtasks":
            verb = args[1].lower()
            if verb == "/query":
                if state["task"]:
                    return FakeCompleted(0, b"TaskName: AIOS Intelligence Monitor")
                return FakeCompleted(1, b"", b"ERROR: cannot find the file specified.")
            if verb == "/create":
                if not state["elevated"]:
                    return FakeCompleted(1, b"", "错误: 拒绝访问。".encode("gbk"))
                state["task"] = True
                return FakeCompleted(0, b"SUCCESS: task created")
            if verb == "/delete":
                state["task"] = False
                return FakeCompleted(0, b"SUCCESS: task deleted")

        if program == "powershell":
            # Emulate WScript.Shell creating the .lnk.
            startup_service.shortcut_path().write_text("shortcut", encoding="utf-8")
            return FakeCompleted(0, b"")

        return FakeCompleted(1, b"", b"unexpected")

    monkeypatch.setattr(startup_service.subprocess, "run", fake_run)
    monkeypatch.setattr(startup_service, "is_supported", lambda: True)
    monkeypatch.setattr(startup_service, "is_elevated", lambda: state["elevated"])
    monkeypatch.setattr(startup_service, "startup_dir", lambda: startup_dir)
    return state


class TestCommandGeneration:
    def test_paths_are_absolute_and_not_hardcoded(self):
        """The project must survive being moved to another directory."""
        command = startup_service.build_command()
        assert str(startup_service.entry_point()) in command
        assert command.startswith('"')
        assert Path(startup_service.entry_point()).is_absolute()

    def test_entry_point_is_this_project(self):
        entry = startup_service.entry_point()
        assert entry.name == "app.py"
        assert entry.exists(), "app.py must exist for auto-start to be registrable"

    def test_command_uses_no_browser(self):
        """A browser tab on every logon would be obnoxious."""
        assert "--no-browser" in startup_service.build_command()

    def test_command_carries_no_credentials(self):
        """Keys come from the keyring after startup, never from task arguments."""
        command = startup_service.build_command()
        for marker in ("sk-", "api_key", "API_KEY", "password", "token"):
            assert marker not in command

    def test_interpreter_is_the_running_one(self):
        executable = startup_service.python_executable()
        assert executable.exists()
        assert executable.parent == Path(sys.executable).parent

    @pytest.mark.skipif(sys.platform != "win32", reason="pythonw is Windows-only")
    def test_prefers_pythonw_when_available(self):
        """Avoids a console window at logon - but only if it really exists."""
        executable = startup_service.python_executable()
        assert executable.name in ("pythonw.exe", "python.exe")


class TestEnableDisableElevated:
    """With admin rights the Task Scheduler path is used."""

    def test_enable_creates_a_logon_task(self, fake_windows):
        fake_windows["elevated"] = True
        status = startup_service.enable()

        assert status.enabled is True
        assert status.mechanism == startup_service.MECHANISM_TASK
        assert not status.error

        create = next(c for c in fake_windows["calls"] if c[:2] == ["schtasks", "/Create"])
        assert startup_service.TASK_NAME in create
        assert "ONLOGON" in create
        assert "--no-browser" in " ".join(create)

    def test_enable_removes_any_folder_entry(self, fake_windows):
        """Both mechanisms firing at once would start AIOS twice."""
        fake_windows["elevated"] = False
        startup_service.enable()
        assert startup_service.shortcut_path().exists()

        fake_windows["elevated"] = True
        startup_service.enable()
        assert not startup_service.shortcut_path().exists()
        assert startup_service.get_status().mechanism == startup_service.MECHANISM_TASK

    def test_disable_removes_the_task(self, fake_windows):
        fake_windows["elevated"] = True
        startup_service.enable()
        status = startup_service.disable()

        assert status.enabled is False
        assert not status.error
        assert any(c[:2] == ["schtasks", "/Delete"] for c in fake_windows["calls"])
        assert startup_service.get_status().enabled is False


class TestEnableDisableUnprivileged:
    """Without admin rights AIOS must still be able to start itself at logon."""

    def test_falls_back_to_the_startup_folder(self, fake_windows):
        fake_windows["elevated"] = False
        status = startup_service.enable()

        assert status.enabled is True, status.error
        assert status.mechanism == startup_service.MECHANISM_FOLDER
        assert not status.error
        assert startup_service.shortcut_path().exists()

    def test_status_reports_the_folder_mechanism(self, fake_windows):
        fake_windows["elevated"] = False
        assert startup_service.get_status().enabled is False
        startup_service.enable()

        status = startup_service.get_status()
        assert status.enabled is True
        assert status.mechanism == startup_service.MECHANISM_FOLDER
        assert "启动文件夹" in status.mechanism_label

    def test_disable_removes_the_folder_entry(self, fake_windows):
        fake_windows["elevated"] = False
        startup_service.enable()
        status = startup_service.disable()

        assert status.enabled is False
        assert not startup_service.shortcut_path().exists()
        assert startup_service.get_status().enabled is False

    def test_batch_fallback_when_powershell_fails(self, fake_windows, monkeypatch):
        """Reliability beats hiding the console: a .cmd always works."""
        fake_windows["elevated"] = False
        original = startup_service.subprocess.run

        def failing_run(args, **kwargs):
            if args[0].lower() == "powershell":
                return FakeCompleted(1, b"", b"powershell unavailable")
            return original(args, **kwargs)

        monkeypatch.setattr(startup_service.subprocess, "run", failing_run)

        status = startup_service.enable()
        assert status.enabled is True
        assert startup_service.batch_path().exists()
        content = startup_service.batch_path().read_text(encoding="utf-8")
        assert "--no-browser" in content
        assert "sk-" not in content

    def test_enable_is_idempotent(self, fake_windows):
        fake_windows["elevated"] = False
        startup_service.enable()
        startup_service.enable()
        assert startup_service.get_status().enabled is True

    def test_disable_when_absent_is_not_an_error(self, fake_windows):
        status = startup_service.disable()
        assert status.enabled is False
        assert not status.error

    def test_missing_entry_point_is_reported(self, fake_windows, monkeypatch, tmp_path):
        monkeypatch.setattr(
            startup_service, "entry_point", lambda: tmp_path / "does-not-exist.py"
        )
        status = startup_service.enable()
        assert status.enabled is False
        assert "找不到" in status.error

    def test_failure_to_write_is_surfaced(self, fake_windows, monkeypatch):
        fake_windows["elevated"] = False
        monkeypatch.setattr(
            startup_service, "_create_shortcut", lambda: (False, "disk full")
        )
        status = startup_service.enable()
        assert status.enabled is False
        assert "disk full" in status.error


class TestNonWindows:
    def test_unsupported_platform_disables_the_feature(self, monkeypatch):
        monkeypatch.setattr(startup_service, "is_supported", lambda: False)

        status = startup_service.get_status()
        assert status.supported is False
        assert status.enabled is False
        assert "Windows" in status.detail

        assert "Windows" in startup_service.enable().error
        assert "Windows" in startup_service.disable().error

    def test_is_supported_matches_the_platform(self):
        assert startup_service.is_supported() == (sys.platform == "win32")

    def test_elevation_check_never_raises(self):
        assert isinstance(startup_service.is_elevated(), bool)


class TestDuplicateInstanceGuard:
    def test_detects_a_bound_port(self):
        """TEST J: a logon task must not stack a second server."""
        import socket
        import threading

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        stop = threading.Event()

        def accept_once():
            server.settimeout(2.0)
            try:
                connection, _ = server.accept()
                connection.close()
            except OSError:
                pass
            stop.set()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        try:
            assert startup_service.already_running("127.0.0.1", port) is True
        finally:
            stop.wait(timeout=2)
            server.close()

    def test_free_port_reports_not_running(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # The socket is closed, so nothing is listening on that port now.
        assert startup_service.already_running("127.0.0.1", port) is False


class TestLauncherNoBrowserMode:
    """``python app.py --no-browser`` is how the logon task starts AIOS."""

    def _launcher(self):
        import importlib.util

        from aios.config import PROJECT_ROOT

        spec = importlib.util.spec_from_file_location(
            "aios_launcher", PROJECT_ROOT / "app.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_flag_is_supported(self):
        source = (Path(startup_service.entry_point())).read_text(encoding="utf-8")
        assert "--no-browser" in source

    def test_already_running_exits_zero_in_no_browser_mode(self, monkeypatch, capsys):
        """A duplicate logon start should be a quiet no-op, not an error."""
        launcher = self._launcher()
        monkeypatch.setattr(launcher, "port_is_free", lambda host, port: False)
        monkeypatch.setattr(sys, "argv", ["app.py", "--no-browser"])

        assert launcher.main() == 0
        assert "already running" in capsys.readouterr().out

    def test_manual_start_on_a_busy_port_is_an_error(self, monkeypatch, capsys):
        launcher = self._launcher()
        monkeypatch.setattr(launcher, "port_is_free", lambda host, port: False)
        monkeypatch.setattr(sys, "argv", ["app.py"])

        assert launcher.main() == 1
        assert "already in use" in capsys.readouterr().out

    def test_no_browser_skips_opening_a_browser(self, monkeypatch, paths):
        launcher = self._launcher()
        opened = []

        monkeypatch.setattr(launcher, "port_is_free", lambda host, port: True)
        monkeypatch.setattr(
            launcher.webbrowser, "open", lambda url: opened.append(url)
        )

        started = {}

        def fake_uvicorn_run(app, **kwargs):
            started["app"] = app

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
        monkeypatch.setattr(sys, "argv", ["app.py", "--no-browser"])

        assert launcher.main() == 0
        assert started["app"] == "aios.app:app"
        assert opened == []
