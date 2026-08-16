from __future__ import annotations

import sys
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
        self.assertIn("Daemon: running (pid 42, http://127.0.0.1:9333/)", lines)
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


if __name__ == "__main__":
    unittest.main()
