from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from web_bridge.cli import (  # noqa: E402
    CliError,
    build_parser,
    build_request_payload,
    load_help_document,
    main,
    send_command,
)


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def payload(self, *argv: str) -> dict[str, object]:
        return build_request_payload(self.parser.parse_args(argv))

    def test_navigate(self) -> None:
        self.assertEqual(
            self.payload(
                "研究任务",
                "navigate",
                "https://example.com",
                "--new-tab",
                "--group-title",
                "资料组",
            ),
            {
                "action": "navigate",
                "args": {
                    "url": "https://example.com",
                    "newTab": True,
                    "group_title": "资料组",
                },
                "session": "研究任务",
            },
        )

    def test_find_tab(self) -> None:
        self.assertEqual(
            self.payload("task", "find_tab", "https://example.com", "--active"),
            {
                "action": "find_tab",
                "args": {"url": "https://example.com", "active": True},
                "session": "task",
            },
        )

    def test_simple_actions(self) -> None:
        for command, action in (
            ("snapshot", "snapshot"),
            ("list_tabs", "list_tabs"),
            ("close_tab", "close_tab"),
            ("close_session", "close_session"),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    self.payload("task", command),
                    {"action": action, "args": {}, "session": "task"},
                )

    def test_click_fill_and_evaluate(self) -> None:
        self.assertEqual(
            self.payload("task", "click", "@e42")["args"],
            {"selector": "@e42"},
        )
        self.assertEqual(
            self.payload("task", "fill", "@e42", "中文内容")["args"],
            {"selector": "@e42", "value": "中文内容"},
        )
        self.assertEqual(
            self.payload("task", "evaluate", "location.href")["args"],
            {"code": "location.href"},
        )

    def test_cdp_input_tools(self) -> None:
        self.assertEqual(
            self.payload("task", "mouse_click", "@e42"),
            {
                "action": "mouse_click",
                "args": {"selector": "@e42"},
                "session": "task",
            },
        )
        self.assertEqual(
            self.payload("task", "key_type", "中文")["args"],
            {"text": "中文"},
        )
        self.assertEqual(
            self.payload("task", "send_keys", "Mod+A", "--repeat", "2")["args"],
            {"keys": "Mod+A", "repeat": 2},
        )

    def test_multiword_tools_only_accept_underscored_cli_names(self) -> None:
        for argv in (
            ("find-tab", "https://example.com"),
            ("mouse-click", "@e42"),
            ("key-type", "text"),
            ("send-keys", "Enter"),
            ("save-as-pdf",),
            ("list-tabs",),
            ("close-tab",),
            ("close-session",),
        ):
            with self.subTest(command=argv[0]):
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit):
                    self.parser.parse_args(["task", *argv])

    def test_cdp(self) -> None:
        self.assertEqual(
            self.payload(
                "task",
                "cdp",
                "Page.captureScreenshot",
                '{"format":"png"}',
            )["args"],
            {"method": "Page.captureScreenshot", "params": {"format": "png"}},
        )

    def test_screenshot(self) -> None:
        self.assertEqual(
            self.payload(
                "task",
                "screenshot",
                "--format",
                "jpeg",
                "--quality",
                "60",
                "--selector",
                "@e1",
                "--path",
                "/tmp/page.jpg",
            )["args"],
            {
                "format": "jpeg",
                "quality": 60,
                "selector": "@e1",
                "path": "/tmp/page.jpg",
            },
        )

    def test_network_and_upload(self) -> None:
        self.assertEqual(
            self.payload(
                "task",
                "network",
                "detail",
                "--filter",
                "api",
                "--request-id",
                "123",
            )["args"],
            {"cmd": "detail", "filter": "api", "requestId": "123"},
        )
        self.assertEqual(
            self.payload("task", "upload", "@e9", "/tmp/a", "/tmp/b")["args"],
            {"selector": "@e9", "files": ["/tmp/a", "/tmp/b"]},
        )

    def test_save_as_pdf(self) -> None:
        self.assertEqual(
            self.payload(
                "task",
                "save_as_pdf",
                "--paper-format",
                "a4",
                "--landscape",
                "--scale",
                "0.8",
                "--no-print-background",
                "--path",
                "/tmp/page.pdf",
            ),
            {
                "action": "save_as_pdf",
                "args": {
                    "paper_format": "a4",
                    "scale": 0.8,
                    "path": "/tmp/page.pdf",
                    "landscape": True,
                    "print_background": False,
                },
                "session": "task",
            },
        )


class TransportTests(unittest.TestCase):
    def test_send_command_uses_utf8_compact_json(self) -> None:
        class Response:
            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"success":true}'

        payload = {
            "action": "fill",
            "args": {"selector": "@e1", "value": "中文"},
            "session": "task",
        }
        with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            result = send_command("http://127.0.0.1:10086/command", payload, 12)

        self.assertEqual(result, {"success": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            payload,
        )
        self.assertNotIn(b"\n", request.data)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)

    def test_connection_error_is_wrapped(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaisesRegex(CliError, "daemon 与浏览器扩展已经启动"):
                send_command(
                    "http://127.0.0.1:10086/command",
                    {"action": "snapshot", "args": {}, "session": "task"},
                    1,
                )

    def test_main_prints_compact_json(self) -> None:
        output = io.StringIO()
        with patch("web_bridge.cli.send_command", return_value={"text": "中文"}):
            with redirect_stdout(output):
                main(["task", "snapshot"])
        self.assertEqual(output.getvalue(), '{"text":"中文"}\n')

    def test_default_endpoint_targets_existing_bridge(self) -> None:
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(["task", "snapshot"]).endpoint,
            "http://127.0.0.1:10086/command",
        )


class HelpParityTests(unittest.TestCase):
    GUIDE_HEADINGS = (
        "Web Bridge",
        "Tools",
        "Tabs and the current tab",
        "Call Format",
        "Sessions",
        "Screenshots",
        "Prefer snapshot over CSS/JS selectors",
        "Evaluate Tips",
        "Text input — use `fill`",
        "Form submit / special keys",
        "Save the current page as PDF",
        "Known limitations",
        "If a tool call fails (daemon or extension not ready)",
        "Version mismatches",
    )

    def test_help_keeps_all_reference_sections_in_order(self) -> None:
        document = load_help_document()
        positions = [document.index(heading) for heading in self.GUIDE_HEADINGS]
        self.assertEqual(positions, sorted(positions))

    def test_help_keeps_every_reference_action(self) -> None:
        document = load_help_document()
        for action in (
            "navigate",
            "find_tab",
            "snapshot",
            "click",
            "fill",
            "mouse_click",
            "evaluate",
            "cdp",
            "key_type",
            "send_keys",
            "screenshot",
            "network",
            "upload",
            "save_as_pdf",
            "list_tabs",
            "close_tab",
            "close_session",
        ):
            with self.subTest(action=action):
                self.assertIn(action, document)

    def test_help_replaces_old_invocation_style(self) -> None:
        document = load_help_document()
        self.assertNotIn("curl", document.lower())
        self.assertNotIn("uv run web ", document)
        old_brand = "".join(("Ki", "mi"))
        self.assertNotIn(old_brand, document)
        self.assertIn("webbridge SESSION ACTION", document)
        self.assertIn("webbridge my-task screenshot", document)
        self.assertNotIn("mouse-click", document)
        self.assertNotIn("key-type", document)
        self.assertNotIn("send-keys", document)
        self.assertNotIn("find-tab", document)
        self.assertNotIn("save-as-pdf", document)
        self.assertNotIn("list-tabs", document)
        self.assertNotIn("close-tab", document)
        self.assertNotIn("close-session", document)

    def test_help_has_no_extra_operations_appendix(self) -> None:
        document = load_help_document()
        self.assertNotIn("Operations reference: daemon lifecycle and recovery", document)
        self.assertNotIn("/status JSON fields", document)


if __name__ == "__main__":
    unittest.main()
