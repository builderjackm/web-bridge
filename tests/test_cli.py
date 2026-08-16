from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from web_bridge import cli  # noqa: E402


class LifecycleCliTests(unittest.TestCase):
    @patch.object(
        cli,
        "_status_payload",
        return_value={
            "name": "webbridge",
            "pid": 42,
            "extensionConnected": True,
            "cdpClients": 2,
            "targets": 3,
            "pageTargets": 2,
        },
    )
    @patch("builtins.print")
    def test_status_reports_daemon_and_extension(self, output: Mock, _: Mock) -> None:
        self.assertEqual(cli.run(["status"]), 0)
        lines = [call.args[0] for call in output.call_args_list]
        self.assertIn("Daemon: running (pid 42, http://127.0.0.1:9222/)", lines)
        self.assertIn("Extension: connected", lines)
        self.assertIn("Page targets: 2", lines)

    @patch.object(cli, "_write_pid")
    @patch.object(
        cli,
        "_status_payload",
        return_value={"name": "webbridge", "pid": 42},
    )
    @patch("builtins.print")
    def test_start_is_idempotent(self, output: Mock, _: Mock, write_pid: Mock) -> None:
        self.assertEqual(cli.run(["start"]), 0)
        write_pid.assert_called_once_with(42)
        output.assert_called_once_with("WebBridge daemon is already running (pid 42).")

    @patch.object(cli.time, "sleep")
    @patch.object(cli, "_write_pid")
    @patch.object(cli, "_spawn_daemon")
    @patch.object(
        cli,
        "_status_payload",
        side_effect=[
            None,
            {"name": "webbridge", "pid": 42, "extensionConnected": False},
        ],
    )
    @patch("builtins.print")
    def test_start_records_the_daemon_pid(
        self,
        output: Mock,
        _: Mock,
        spawn: Mock,
        write_pid: Mock,
        __: Mock,
    ) -> None:
        spawn.return_value.pid = 24
        spawn.return_value.poll.return_value = None

        self.assertEqual(cli.run(["start"]), 0)

        self.assertEqual([call.args[0] for call in write_pid.call_args_list], [24, 42])
        self.assertIn("WebBridge daemon started (pid 42).", output.call_args_list[0].args)

    @patch.object(cli, "_remove_pid")
    @patch.object(cli, "_read_pid", return_value=None)
    @patch.object(cli, "_status_payload", return_value=None)
    @patch("builtins.print")
    def test_stop_is_idempotent(
        self, output: Mock, _: Mock, __: Mock, remove_pid: Mock
    ) -> None:
        self.assertEqual(cli.run(["stop"]), 0)
        remove_pid.assert_called_once_with()
        output.assert_called_once_with("WebBridge daemon is not running.")

    @patch.object(cli, "start", return_value=0)
    @patch.object(cli, "stop", return_value=0)
    def test_restart_stops_then_starts(self, stop: Mock, start: Mock) -> None:
        self.assertEqual(cli.run(["restart"]), 0)
        stop.assert_called_once_with()
        start.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "Windows process API test")
    def test_windows_pid_alive_is_nondestructive(self) -> None:
        self.assertTrue(cli._pid_alive(os.getpid()))

        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=5)
        self.assertFalse(cli._pid_alive(process.pid))

    def test_windows_spawn_does_not_open_a_console_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            log_file = state_dir / "daemon.log"
            python = state_dir / "Scripts" / "python.exe"
            pythonw = python.with_name("pythonw.exe")
            pythonw.parent.mkdir()
            pythonw.touch()
            with (
                patch.object(cli, "STATE_DIR", state_dir),
                patch.object(cli, "LOG_FILE", log_file),
                patch.object(cli.os, "name", "nt"),
                patch.object(cli.sys, "executable", str(python)),
                patch.object(cli.subprocess, "Popen") as popen,
            ):
                cli._spawn_daemon()

        args, kwargs = popen.call_args
        self.assertEqual(
            args[0], [str(pythonw), "-m", "web_bridge.daemon"]
        )
        self.assertEqual(kwargs["creationflags"], cli.WINDOWS_CREATE_NO_WINDOW)
        self.assertNotIn("start_new_session", kwargs)

    def test_windows_spawn_falls_back_when_pythonw_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            python = state_dir / "python.exe"
            with (
                patch.object(cli, "STATE_DIR", state_dir),
                patch.object(cli, "LOG_FILE", state_dir / "daemon.log"),
                patch.object(cli.os, "name", "nt"),
                patch.object(cli.sys, "executable", str(python)),
                patch.object(cli.subprocess, "Popen") as popen,
            ):
                cli._spawn_daemon()

        args, kwargs = popen.call_args
        self.assertEqual(args[0], [str(python), "-m", "web_bridge.daemon"])
        self.assertEqual(kwargs["creationflags"], cli.WINDOWS_CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
