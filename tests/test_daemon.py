from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import ClientSession, WSMsgType, web

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from web_bridge.daemon import create_app  # noqa: E402


class FakeExtension:
    def __init__(self, session: ClientSession, base_url: str) -> None:
        self.session = session
        self.base_url = base_url
        self.socket = None
        self.task = None
        self.commands: List[Dict[str, Any]] = []
        self.tab = {
            "id": 7,
            "windowId": 1,
            "title": "Example",
            "url": "https://example.com/",
        }
        self.target = {
            "id": "page-7",
            "targetId": "page-7",
            "tabId": 7,
            "type": "page",
            "title": "Example",
            "url": "https://example.com/",
            "attached": False,
            "browserContextId": "default",
        }

    async def start(self) -> None:
        self.socket = await self.session.ws_connect(f"{self.base_url}/extension")
        self.task = asyncio.create_task(self._serve())
        await self.emit(
            "bridge.ready",
            [
                {
                    "tabs": [self.tab],
                    "targets": [self.target],
                    "browser": {
                        "userAgent": ("Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36")
                    },
                }
            ],
        )
        for _ in range(50):
            async with self.session.get(f"{self.base_url}/") as response:
                status = await response.json()
            if status["extensionConnected"] and status["targets"] == 1:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("extension did not become ready")

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()
        if self.task is not None:
            await self.task

    async def emit(self, method: str, params: List[Any]) -> None:
        assert self.socket is not None
        await self.socket.send_json({"method": method, "params": params})

    async def _serve(self) -> None:
        assert self.socket is not None
        async for message in self.socket:
            if message.type != WSMsgType.TEXT:
                continue
            request = json.loads(message.data)
            self.commands.append(request)
            try:
                result = self._result(request["method"], request.get("params", []))
                await self.socket.send_json({"id": request["id"], "result": result})
            except Exception as exc:  # pragma: no cover - test diagnostic
                await self.socket.send_json(
                    {"id": request["id"], "error": {"message": str(exc)}}
                )

    def _result(self, method: str, params: List[Any]) -> Any:
        if method in {
            "chrome.debugger.attach",
            "chrome.debugger.detach",
            "chrome.tabs.remove",
        }:
            return None
        if method == "chrome.debugger.getTargets":
            return [self.target]
        if method == "chrome.debugger.sendCommand":
            cdp_method = params[1]
            if cdp_method == "Target.getTargetInfo":
                info = dict(self.target)
                info["attached"] = True
                return {"targetInfo": info}
            if cdp_method == "Runtime.evaluate":
                return {
                    "result": {
                        "type": "string",
                        "value": "forwarded",
                    }
                }
            return {}
        if method == "chrome.tabs.update":
            return self.tab
        if method == "chrome.windows.update":
            return {"id": 1, "focused": True}
        if method == "chrome.tabs.create":
            return self.tab
        raise AssertionError(f"unexpected extension RPC: {method}")


class DaemonIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runner = web.AppRunner(create_app())
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        server = self.site._server
        assert server is not None and server.sockets
        self.port = server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.session = ClientSession()
        self.extension = FakeExtension(self.session, self.base_url)
        await self.extension.start()

    async def asyncTearDown(self) -> None:
        await self.extension.close()
        await self.session.close()
        await self.runner.cleanup()

    async def test_standard_discovery(self) -> None:
        async with self.session.get(f"{self.base_url}/") as response:
            status = await response.json()
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["pageTargets"], 1)

        for path in ("/json/version", "/json/version/"):
            async with self.session.get(f"{self.base_url}{path}") as response:
                self.assertEqual(response.status, 200)
                version = await response.json()
            self.assertEqual(version["Protocol-Version"], "1.3")
            self.assertEqual(
                version["webSocketDebuggerUrl"],
                f"ws://127.0.0.1:{self.port}/devtools/browser/webbridge",
            )

        for path in ("/json", "/json/", "/json/list", "/json/list/"):
            async with self.session.get(f"{self.base_url}{path}") as response:
                self.assertEqual(response.status, 200)
                targets = await response.json()
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0]["id"], "page-7")
            self.assertEqual(
                targets[0]["webSocketDebuggerUrl"],
                f"ws://127.0.0.1:{self.port}/devtools/page/page-7",
            )

    async def test_browser_session_routes_commands_and_events(self) -> None:
        cdp = await self.session.ws_connect(
            f"{self.base_url}/devtools/browser/webbridge"
        )
        await cdp.send_json({"id": 1, "method": "Browser.getVersion"})
        version_response = await cdp.receive_json()
        self.assertEqual(version_response["result"]["product"], "Chrome/140.0.0.0")

        await cdp.send_json(
            {
                "id": 11,
                "method": "Browser.setDownloadBehavior",
                "params": {"behavior": "allowAndName", "eventsEnabled": True},
            }
        )
        self.assertEqual(await cdp.receive_json(), {"id": 11, "result": {}})

        await cdp.send_json(
            {
                "id": 2,
                "method": "Target.setAutoAttach",
                "params": {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                },
            }
        )
        first = await cdp.receive_json()
        second = await cdp.receive_json()
        messages = [first, second]
        attached_event = next(
            message
            for message in messages
            if message.get("method") == "Target.attachedToTarget"
        )
        self.assertIn({"id": 2, "result": {}}, messages)
        session_id = attached_event["params"]["sessionId"]

        await cdp.send_json(
            {
                "id": 3,
                "sessionId": session_id,
                "method": "Runtime.evaluate",
                "params": {"expression": "document.title"},
            }
        )
        evaluate_response = await cdp.receive_json()
        self.assertEqual(evaluate_response["result"]["result"]["value"], "forwarded")
        self.assertEqual(evaluate_response["sessionId"], session_id)

        await self.extension.emit(
            "chrome.debugger.onEvent",
            [
                {"tabId": 7},
                "Runtime.consoleAPICalled",
                {"type": "log", "args": []},
            ],
        )
        event = await cdp.receive_json()
        self.assertEqual(event["method"], "Runtime.consoleAPICalled")
        self.assertEqual(event["sessionId"], session_id)
        await cdp.close()

    async def test_page_websocket_is_a_transparent_cdp_channel(self) -> None:
        cdp = await self.session.ws_connect(f"{self.base_url}/devtools/page/page-7")
        await cdp.send_json(
            {
                "id": 9,
                "method": "Runtime.evaluate",
                "params": {"expression": "location.href"},
            }
        )
        response = await cdp.receive_json()
        self.assertEqual(response["id"], 9)
        self.assertEqual(response["result"]["result"]["value"], "forwarded")
        await cdp.close()

    async def test_extension_diagnostics_are_written_to_the_daemon_log(self) -> None:
        record = {
            "timestamp": "2026-08-16T08:00:00.000Z",
            "instanceId": "worker-test",
            "event": "socket.close",
            "details": {"code": 1006, "wasClean": False},
        }
        with self.assertLogs("webbridge", level="INFO") as captured:
            await self.extension.emit("bridge.log", [record])
            for _ in range(50):
                if any("worker-test" in line for line in captured.output):
                    break
                await asyncio.sleep(0.01)

        self.assertTrue(any("worker-test" in line for line in captured.output))
        self.assertTrue(any("socket.close" in line for line in captured.output))


class ExtensionManifestTests(unittest.TestCase):
    def test_extension_is_minimal_and_targets_the_daemon(self) -> None:
        manifest = json.loads(
            (PROJECT_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(
            set(manifest["permissions"]),
            {"alarms", "debugger", "storage", "tabs"},
        )
        self.assertEqual(manifest["action"]["default_popup"], "popup.html")
        transport = (PROJECT_ROOT / "extension" / "background.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("ws://127.0.0.1:9222/extension", transport)
        self.assertIn("bridge.ping", transport)
        self.assertIn("chrome.alarms", transport)
        self.assertIn("chrome.storage.local", transport)
        self.assertIn("bridge.logs", transport)
        self.assertFalse((PROJECT_ROOT / "extension" / "offscreen.html").exists())
        self.assertFalse((PROJECT_ROOT / "extension" / "offscreen.js").exists())
        popup = (PROJECT_ROOT / "extension" / "popup.js").read_text(encoding="utf-8")
        self.assertIn("http://127.0.0.1:9222/", popup)

    def test_old_product_and_command_protocol_are_removed(self) -> None:
        old_brand = "".join(("ki", "mi"))
        old_endpoint = "".join(("100", "86/command"))
        for path in PROJECT_ROOT.rglob("*"):
            ignored = {".git", ".venv", ".pytest_cache", "__pycache__", "dist"}
            if (
                not path.is_file()
                or ignored.intersection(path.parts)
                or path.name == "uv.lock"
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            self.assertNotIn(old_brand, text, str(path))
            self.assertNotIn(old_endpoint, text, str(path))


if __name__ == "__main__":
    unittest.main()
