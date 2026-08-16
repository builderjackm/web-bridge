"""Expose an extension-controlled Chrome instance as a standard CDP endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Dict, Iterable, List, Optional, Set

from aiohttp import WSMsgType, web

from . import __version__

HOST = "127.0.0.1"
PORT = 9222
PROTOCOL_VERSION = "1.3"
RPC_TIMEOUT = 120.0
LOGGER = logging.getLogger("webbridge")


class BridgeError(RuntimeError):
    """An error safe to return to a local CDP client."""


class ExtensionConnection:
    """Small request/response channel over the extension WebSocket."""

    def __init__(self, socket: web.WebSocketResponse) -> None:
        self.socket = socket
        self._ids = count(1)
        self._pending: Dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()

    async def call(self, method: str, params: Optional[List[Any]] = None) -> Any:
        if self.socket.closed:
            raise BridgeError("browser extension is disconnected")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "method": method, "params": params or []}
        try:
            async with self._send_lock:
                await self.socket.send_json(payload)
            return await asyncio.wait_for(future, timeout=RPC_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise BridgeError(f"extension request timed out: {method}") from exc
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, payload: Dict[str, Any]) -> bool:
        request_id = payload.get("id")
        if not isinstance(request_id, int):
            return False
        future = self._pending.get(request_id)
        if future is None or future.done():
            return True
        error = payload.get("error")
        if error:
            if isinstance(error, dict):
                message = str(error.get("message", error))
            else:
                message = str(error)
            future.set_exception(BridgeError(message))
        else:
            future.set_result(payload.get("result"))
        return True

    def reject_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeError(reason))
        self._pending.clear()


@dataclass
class AttachedTab:
    tab_id: int
    target_info: Dict[str, Any]
    consumers: Set[int] = field(default_factory=set)
    child_sessions: Set[str] = field(default_factory=set)


@dataclass(eq=False)
class CDPClient:
    identifier: int
    socket: web.WebSocketResponse
    mode: str
    page_target_id: Optional[str] = None
    sessions_by_tab: Dict[int, str] = field(default_factory=dict)
    tabs_by_session: Dict[str, int] = field(default_factory=dict)
    discover_targets: bool = False
    auto_attach: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: Dict[str, Any]) -> None:
        if self.socket.closed:
            return
        async with self.send_lock:
            await self.socket.send_json(payload)


class BridgeState:
    def __init__(self) -> None:
        self.extension: Optional[ExtensionConnection] = None
        self.tabs: Dict[int, Dict[str, Any]] = {}
        self.targets: Dict[str, Dict[str, Any]] = {}
        self.target_to_tab: Dict[str, int] = {}
        self.attached_tabs: Dict[int, AttachedTab] = {}
        self.clients: Set[CDPClient] = set()
        self.browser: Dict[str, Any] = {}
        self._client_ids = count(1)
        self._session_ids = count(1)
        self._attach_locks: Dict[int, asyncio.Lock] = {}

    @property
    def extension_connected(self) -> bool:
        return self.extension is not None and not self.extension.socket.closed

    def require_extension(self) -> ExtensionConnection:
        if not self.extension_connected or self.extension is None:
            raise BridgeError("browser extension is not connected")
        return self.extension

    def version_payload(self, host: str) -> Dict[str, Any]:
        user_agent = str(self.browser.get("userAgent", "Chrome/Extension-Bridge"))
        match = re.search(r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", user_agent)
        chrome_version = match.group(1) if match else "0.0.0.0"
        return {
            "Browser": f"Chrome/{chrome_version}",
            "Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": user_agent,
            "V8-Version": "",
            "WebKit-Version": "",
            "webSocketDebuggerUrl": f"ws://{host}/devtools/browser/webbridge",
        }

    def browser_version_result(self) -> Dict[str, Any]:
        user_agent = str(self.browser.get("userAgent", "Chrome/Extension-Bridge"))
        match = re.search(r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", user_agent)
        chrome_version = match.group(1) if match else "0.0.0.0"
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "product": f"Chrome/{chrome_version}",
            "revision": "@webbridge",
            "userAgent": user_agent,
            "jsVersion": "",
        }

    def register_client(
        self,
        socket: web.WebSocketResponse,
        mode: str,
        page_target_id: Optional[str] = None,
    ) -> CDPClient:
        client = CDPClient(
            identifier=next(self._client_ids),
            socket=socket,
            mode=mode,
            page_target_id=page_target_id,
        )
        self.clients.add(client)
        return client

    async def release_client(self, client: CDPClient) -> None:
        self.clients.discard(client)
        for tab_id in list(client.sessions_by_tab):
            await self._release_tab(client, tab_id)
        for attached in list(self.attached_tabs.values()):
            if client.identifier in attached.consumers:
                await self._release_tab(client, attached.tab_id)

    async def install_extension(
        self, connection: ExtensionConnection, ready: Optional[Dict[str, Any]] = None
    ) -> None:
        previous = self.extension
        if previous is not None and previous is not connection:
            previous.reject_all("browser extension reconnected")
            await previous.socket.close(code=1000, message=b"replaced")
        self.extension = connection
        self.attached_tabs.clear()
        if ready:
            self._apply_ready(ready)

    async def remove_extension(self, connection: ExtensionConnection) -> None:
        if self.extension is not connection:
            return
        connection.reject_all("browser extension disconnected")
        self.extension = None
        self.attached_tabs.clear()
        for client in list(self.clients):
            if not client.socket.closed:
                await client.socket.close(
                    code=1011, message=b"browser extension disconnected"
                )

    def _apply_ready(self, payload: Dict[str, Any]) -> None:
        self.tabs = {
            int(tab["id"]): tab
            for tab in payload.get("tabs", [])
            if isinstance(tab, dict) and isinstance(tab.get("id"), int)
        }
        self.targets.clear()
        self.target_to_tab.clear()
        for target in payload.get("targets", []):
            self._remember_target(target)
        browser = payload.get("browser")
        self.browser = browser if isinstance(browser, dict) else {}

    def _remember_target(self, target: Any) -> None:
        if not isinstance(target, dict):
            return
        target_id = target.get("id") or target.get("targetId")
        tab_id = target.get("tabId")
        if not isinstance(target_id, str):
            return
        normalized = dict(target)
        normalized["targetId"] = target_id
        normalized.setdefault("type", "page")
        normalized.setdefault("title", "")
        normalized.setdefault("url", "")
        normalized.setdefault("attached", False)
        self.targets[target_id] = normalized
        if isinstance(tab_id, int):
            self.target_to_tab[target_id] = tab_id

    def target_info(self, target_id: str) -> Optional[Dict[str, Any]]:
        target = self.targets.get(target_id)
        return dict(target) if target else None

    def discovery_targets(self, host: str) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for target_id, info in self.targets.items():
            if info.get("type") not in {"page", "webview", "other"}:
                continue
            result.append(
                {
                    "description": "",
                    "devtoolsFrontendUrl": "",
                    "id": target_id,
                    "title": info.get("title", ""),
                    "type": info.get("type", "page"),
                    "url": info.get("url", ""),
                    "webSocketDebuggerUrl": (f"ws://{host}/devtools/page/{target_id}"),
                }
            )
        return result

    async def handle_extension_event(self, method: str, params: List[Any]) -> None:
        if method == "bridge.ready":
            if params and isinstance(params[0], dict):
                self._apply_ready(params[0])
            return
        if method == "bridge.ping":
            return
        if method == "chrome.tabs.onCreated":
            if params and isinstance(params[0], dict):
                tab = params[0]
                if isinstance(tab.get("id"), int):
                    self.tabs[tab["id"]] = tab
                    asyncio.create_task(self._auto_attach_tab(tab["id"]))
            return
        if method == "chrome.tabs.onUpdated":
            await self._on_tab_updated(params)
            return
        if method == "chrome.tabs.onRemoved":
            if params and isinstance(params[0], int):
                await self._on_tab_removed(params[0])
            return
        if method == "chrome.debugger.onEvent" and len(params) >= 3:
            source, cdp_method, cdp_params = params[:3]
            if isinstance(source, dict) and isinstance(cdp_method, str):
                await self._forward_debugger_event(
                    source,
                    cdp_method,
                    cdp_params if isinstance(cdp_params, dict) else {},
                )
            return
        if method == "chrome.debugger.onDetach" and params:
            source = params[0]
            if isinstance(source, dict) and isinstance(source.get("tabId"), int):
                await self._on_debugger_detached(source["tabId"])

    async def _on_tab_updated(self, params: List[Any]) -> None:
        if len(params) < 3 or not isinstance(params[0], int):
            return
        tab_id = params[0]
        tab = params[2] if isinstance(params[2], dict) else {}
        self.tabs[tab_id] = tab
        attached = self.attached_tabs.get(tab_id)
        if attached:
            info = attached.target_info
            info["title"] = tab.get("title", info.get("title", ""))
            info["url"] = tab.get("url", info.get("url", ""))
            self.targets[info["targetId"]] = dict(info)
            await self._broadcast_root_event(
                "Target.targetInfoChanged",
                {"targetInfo": dict(info)},
                discover_only=True,
            )

    async def _on_tab_removed(self, tab_id: int) -> None:
        self.tabs.pop(tab_id, None)
        attached = self.attached_tabs.pop(tab_id, None)
        target_ids = [
            target_id
            for target_id, mapped_tab in self.target_to_tab.items()
            if mapped_tab == tab_id
        ]
        for target_id in target_ids:
            self.target_to_tab.pop(target_id, None)
            self.targets.pop(target_id, None)
        for client in list(self.clients):
            session_id = client.sessions_by_tab.pop(tab_id, None)
            if session_id:
                client.tabs_by_session.pop(session_id, None)
                await client.send(
                    {
                        "method": "Target.detachedFromTarget",
                        "params": {
                            "sessionId": session_id,
                            "targetId": (
                                attached.target_info.get("targetId")
                                if attached
                                else None
                            ),
                        },
                    }
                )
        for target_id in target_ids:
            await self._broadcast_root_event(
                "Target.targetDestroyed", {"targetId": target_id}, discover_only=True
            )

    async def _on_debugger_detached(self, tab_id: int) -> None:
        attached = self.attached_tabs.pop(tab_id, None)
        if not attached:
            return
        for client in list(self.clients):
            session_id = client.sessions_by_tab.pop(tab_id, None)
            if session_id:
                client.tabs_by_session.pop(session_id, None)
                await client.send(
                    {
                        "method": "Target.detachedFromTarget",
                        "params": {
                            "sessionId": session_id,
                            "targetId": attached.target_info.get("targetId"),
                        },
                    }
                )

    async def _forward_debugger_event(
        self, source: Dict[str, Any], method: str, params: Dict[str, Any]
    ) -> None:
        tab_id = source.get("tabId")
        if not isinstance(tab_id, int):
            return
        attached = self.attached_tabs.get(tab_id)
        if attached and method == "Target.attachedToTarget":
            child_session = params.get("sessionId")
            if isinstance(child_session, str):
                attached.child_sessions.add(child_session)
        elif attached and method == "Target.detachedFromTarget":
            child_session = params.get("sessionId")
            if isinstance(child_session, str):
                attached.child_sessions.discard(child_session)

        for client in list(self.clients):
            if client.mode == "page" and client.page_target_id:
                if self.target_to_tab.get(client.page_target_id) != tab_id:
                    continue
                payload: Dict[str, Any] = {"method": method, "params": params}
                source_session = source.get("sessionId")
                if isinstance(source_session, str):
                    payload["sessionId"] = source_session
                await client.send(payload)
                continue

            if method.startswith("Browser."):
                await client.send({"method": method, "params": params})
                continue
            top_session = client.sessions_by_tab.get(tab_id)
            if not top_session:
                continue
            payload = {"method": method, "params": params}
            source_session = source.get("sessionId")
            payload["sessionId"] = (
                source_session if isinstance(source_session, str) else top_session
            )
            await client.send(payload)

    async def _broadcast_root_event(
        self, method: str, params: Dict[str, Any], discover_only: bool = False
    ) -> None:
        for client in list(self.clients):
            if client.mode != "browser":
                continue
            if discover_only and not client.discover_targets:
                continue
            await client.send({"method": method, "params": params})

    async def _auto_attach_tab(self, tab_id: int) -> None:
        clients = [
            client
            for client in list(self.clients)
            if client.mode == "browser" and client.auto_attach
        ]
        if not clients:
            return
        try:
            for client in clients:
                await self.attach_for_client(client, tab_id)
        except BridgeError:
            LOGGER.debug("could not auto-attach tab %s", tab_id, exc_info=True)

    async def _attach_tab(self, tab_id: int) -> AttachedTab:
        lock = self._attach_locks.setdefault(tab_id, asyncio.Lock())
        async with lock:
            existing = self.attached_tabs.get(tab_id)
            if existing:
                return existing
            extension = self.require_extension()
            await extension.call(
                "chrome.debugger.attach", [{"tabId": tab_id}, PROTOCOL_VERSION]
            )
            result = await extension.call(
                "chrome.debugger.sendCommand",
                [{"tabId": tab_id}, "Target.getTargetInfo", {}],
            )
            if not isinstance(result, dict) or not isinstance(
                result.get("targetInfo"), dict
            ):
                raise BridgeError(
                    f"Target.getTargetInfo returned no target for tab {tab_id}"
                )
            info = dict(result["targetInfo"])
            target_id = info.get("targetId")
            if not isinstance(target_id, str):
                raise BridgeError(
                    f"Target.getTargetInfo returned no targetId for tab {tab_id}"
                )
            info.setdefault("type", "page")
            info.setdefault("title", self.tabs.get(tab_id, {}).get("title", ""))
            info.setdefault("url", self.tabs.get(tab_id, {}).get("url", ""))
            info.setdefault("browserContextId", "default")
            info["attached"] = True
            attached = AttachedTab(tab_id=tab_id, target_info=info)
            self.attached_tabs[tab_id] = attached
            remembered = dict(info)
            remembered["tabId"] = tab_id
            self._remember_target(remembered)
            return attached

    async def attach_for_client(self, client: CDPClient, tab_id: int) -> AttachedTab:
        attached = await self._attach_tab(tab_id)
        attached.consumers.add(client.identifier)
        if client.mode == "browser" and tab_id not in client.sessions_by_tab:
            session_id = f"webbridge-{client.identifier}-{next(self._session_ids)}"
            client.sessions_by_tab[tab_id] = session_id
            client.tabs_by_session[session_id] = tab_id
            await client.send(
                {
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": session_id,
                        "targetInfo": dict(attached.target_info),
                        "waitingForDebugger": False,
                    },
                }
            )
        return attached

    async def attach_target_for_client(
        self, client: CDPClient, target_id: str
    ) -> AttachedTab:
        tab_id = self.target_to_tab.get(target_id)
        if tab_id is None:
            await self.refresh_targets()
            tab_id = self.target_to_tab.get(target_id)
        if tab_id is None:
            raise BridgeError(f"unknown target: {target_id}")
        return await self.attach_for_client(client, tab_id)

    async def _release_tab(self, client: CDPClient, tab_id: int) -> None:
        session_id = client.sessions_by_tab.pop(tab_id, None)
        if session_id:
            client.tabs_by_session.pop(session_id, None)
        attached = self.attached_tabs.get(tab_id)
        if not attached:
            return
        attached.consumers.discard(client.identifier)
        if attached.consumers:
            return
        self.attached_tabs.pop(tab_id, None)
        try:
            await self.require_extension().call(
                "chrome.debugger.detach", [{"tabId": tab_id}]
            )
        except BridgeError:
            LOGGER.debug("could not detach tab %s", tab_id, exc_info=True)

    async def refresh_targets(self) -> None:
        result = await self.require_extension().call("chrome.debugger.getTargets", [])
        if not isinstance(result, list):
            return
        for target in result:
            self._remember_target(target)

    def _candidate_tab_ids(self) -> Iterable[int]:
        for tab_id, tab in self.tabs.items():
            url = str(tab.get("url", ""))
            if url.startswith(("chrome-extension://", "devtools://")):
                continue
            yield tab_id

    async def enable_auto_attach(self, client: CDPClient) -> None:
        client.auto_attach = True
        for tab_id in list(self._candidate_tab_ids()):
            try:
                await self.attach_for_client(client, tab_id)
            except BridgeError:
                LOGGER.debug("skipping non-debuggable tab %s", tab_id, exc_info=True)

    async def send_browser_command(self, method: str, params: Dict[str, Any]) -> Any:
        if not self.attached_tabs:
            for tab_id in self._candidate_tab_ids():
                try:
                    await self._attach_tab(tab_id)
                    break
                except BridgeError:
                    continue
        attached = next(iter(self.attached_tabs.values()), None)
        if attached is None:
            raise BridgeError(f"no debuggable tab available for {method}")
        return await self.require_extension().call(
            "chrome.debugger.sendCommand",
            [{"tabId": attached.tab_id}, method, params],
        )

    async def send_tab_command(
        self,
        tab_id: int,
        method: str,
        params: Dict[str, Any],
        child_session_id: Optional[str] = None,
    ) -> Any:
        debuggee: Dict[str, Any] = {"tabId": tab_id}
        if child_session_id:
            debuggee["sessionId"] = child_session_id
        result = await self.require_extension().call(
            "chrome.debugger.sendCommand", [debuggee, method, params]
        )
        attached = self.attached_tabs.get(tab_id)
        if attached and method == "Target.attachToTarget" and isinstance(result, dict):
            child_session = result.get("sessionId")
            if isinstance(child_session, str):
                attached.child_sessions.add(child_session)
        return result

    def _tab_for_any_session(self, session_id: str) -> Optional[int]:
        for tab_id, attached in self.attached_tabs.items():
            if session_id in attached.child_sessions:
                return tab_id
        return None

    async def create_target(self, url: str = "about:blank") -> Dict[str, Any]:
        tab = await self.require_extension().call("chrome.tabs.create", [{"url": url}])
        if not isinstance(tab, dict) or not isinstance(tab.get("id"), int):
            raise BridgeError("chrome.tabs.create returned no tab")
        tab_id = tab["id"]
        self.tabs[tab_id] = tab
        attached = await self._attach_tab(tab_id)
        return dict(attached.target_info)

    async def close_target(self, target_id: str) -> bool:
        tab_id = self.target_to_tab.get(target_id)
        if tab_id is None:
            return False
        await self.require_extension().call("chrome.tabs.remove", [tab_id])
        return True

    async def activate_target(self, target_id: str) -> bool:
        tab_id = self.target_to_tab.get(target_id)
        if tab_id is None:
            return False
        tab = await self.require_extension().call(
            "chrome.tabs.update", [tab_id, {"active": True}]
        )
        if isinstance(tab, dict) and isinstance(tab.get("windowId"), int):
            await self.require_extension().call(
                "chrome.windows.update", [tab["windowId"], {"focused": True}]
            )
        return True

    async def handle_cdp_command(
        self, client: CDPClient, payload: Dict[str, Any]
    ) -> Any:
        method = payload.get("method")
        params = payload.get("params") or {}
        session_id = payload.get("sessionId")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise BridgeError("invalid CDP command")

        if client.mode == "page":
            if not client.page_target_id:
                raise BridgeError("page target is missing")
            attached = await self.attach_target_for_client(
                client, client.page_target_id
            )
            child = session_id if isinstance(session_id, str) else None
            return await self.send_tab_command(attached.tab_id, method, params, child)

        if isinstance(session_id, str):
            tab_id = client.tabs_by_session.get(session_id)
            child_session: Optional[str] = None
            if tab_id is None:
                tab_id = self._tab_for_any_session(session_id)
                child_session = session_id
            if tab_id is None:
                raise BridgeError(f"unknown sessionId: {session_id}")
            return await self.send_tab_command(tab_id, method, params, child_session)

        if method == "Browser.getVersion":
            return self.browser_version_result()
        if method == "Target.setAutoAttach":
            await self.enable_auto_attach(client)
            return {}
        if method == "Target.setDiscoverTargets":
            client.discover_targets = bool(params.get("discover", True))
            if client.discover_targets:
                for info in self.targets.values():
                    await client.send(
                        {
                            "method": "Target.targetCreated",
                            "params": {"targetInfo": dict(info)},
                        }
                    )
            return {}
        if method == "Target.getTargets":
            return {"targetInfos": [dict(info) for info in self.targets.values()]}
        if method == "Target.getTargetInfo":
            requested = params.get("targetId")
            if isinstance(requested, str) and requested in self.targets:
                return {"targetInfo": dict(self.targets[requested])}
            return {
                "targetInfo": {
                    "targetId": "webbridge-browser",
                    "type": "browser",
                    "title": "Chrome",
                    "url": "",
                    "attached": True,
                    "canAccessOpener": False,
                }
            }
        if method == "Target.attachToTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str):
                raise BridgeError("Target.attachToTarget requires targetId")
            attached = await self.attach_target_for_client(client, target_id)
            return {"sessionId": client.sessions_by_tab[attached.tab_id]}
        if method == "Target.detachFromTarget":
            detached_session = params.get("sessionId")
            if isinstance(detached_session, str):
                tab_id = client.tabs_by_session.get(detached_session)
                if tab_id is not None:
                    await self._release_tab(client, tab_id)
                    return {}
            return await self.send_browser_command(method, params)
        if method == "Target.createTarget":
            info = await self.create_target(str(params.get("url", "about:blank")))
            if client.auto_attach:
                tab_id = self.target_to_tab[info["targetId"]]
                await self.attach_for_client(client, tab_id)
            return {"targetId": info["targetId"]}
        if method == "Target.closeTarget":
            target_id = params.get("targetId")
            success = (
                await self.close_target(target_id)
                if isinstance(target_id, str)
                else False
            )
            return {"success": success}
        if method == "Target.activateTarget":
            target_id = params.get("targetId")
            if not isinstance(target_id, str) or not await self.activate_target(
                target_id
            ):
                raise BridgeError(f"unknown target: {target_id}")
            return {}
        return await self.send_browser_command(method, params)


def _request_host(request: web.Request) -> str:
    return request.host or f"{HOST}:{PORT}"


async def status(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    return web.json_response(
        {
            "name": "webbridge",
            "version": __version__,
            "pid": os.getpid(),
            "extensionConnected": state.extension_connected,
            "cdpClients": len(state.clients),
            "targets": len(state.targets),
            "pageTargets": sum(
                1
                for target in state.targets.values()
                if target.get("type") in {"page", "webview"}
            ),
        }
    )


async def json_version(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    if not state.extension_connected:
        raise web.HTTPServiceUnavailable(text="browser extension is not connected")
    return web.json_response(state.version_payload(_request_host(request)))


async def json_list(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    if not state.extension_connected:
        raise web.HTTPServiceUnavailable(text="browser extension is not connected")
    return web.json_response(state.discovery_targets(_request_host(request)))


async def json_new(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    url = request.query_string or "about:blank"
    info = await state.create_target(url)
    target_id = info["targetId"]
    matches = [
        item
        for item in state.discovery_targets(_request_host(request))
        if item["id"] == target_id
    ]
    return web.json_response(matches[0] if matches else info)


async def json_activate(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    if not await state.activate_target(request.match_info["target_id"]):
        raise web.HTTPNotFound(text="unknown target")
    return web.Response(text="Target activated")


async def json_close(request: web.Request) -> web.Response:
    state: BridgeState = request.app[STATE_KEY]
    if not await state.close_target(request.match_info["target_id"]):
        raise web.HTTPNotFound(text="unknown target")
    return web.Response(text="Target is closing")


async def extension_socket(request: web.Request) -> web.WebSocketResponse:
    state: BridgeState = request.app[STATE_KEY]
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    connection = ExtensionConnection(socket)
    await state.install_extension(connection)
    LOGGER.info("browser extension connected")
    try:
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            payload: Dict[str, Any] = {}
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                await socket.close(code=1003, message=b"invalid JSON")
                break
            if not isinstance(payload, dict):
                continue
            if connection.resolve(payload):
                continue
            method = payload.get("method")
            params = payload.get("params", [])
            if isinstance(method, str) and isinstance(params, list):
                await state.handle_extension_event(method, params)
    finally:
        await state.remove_extension(connection)
        LOGGER.info("browser extension disconnected")
    return socket


async def cdp_socket(request: web.Request) -> web.WebSocketResponse:
    state: BridgeState = request.app[STATE_KEY]
    if not state.extension_connected:
        raise web.HTTPServiceUnavailable(text="browser extension is not connected")
    mode = request.match_info["kind"]
    target_id = request.match_info["target_id"]
    if mode == "page" and target_id not in state.targets:
        raise web.HTTPNotFound(text="unknown target")
    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    client = state.register_client(socket, mode, target_id if mode == "page" else None)
    LOGGER.info("CDP client %s connected (%s)", client.identifier, mode)
    if mode == "page":
        try:
            await state.attach_target_for_client(client, target_id)
        except BridgeError as exc:
            await socket.close(code=1011, message=str(exc).encode("utf-8")[:120])
            await state.release_client(client)
            return socket
    tasks: Set[asyncio.Task[None]] = set()

    async def process_message(data: str) -> None:
        payload: Dict[str, Any] = {}
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
                raise BridgeError("CDP command must include an integer id")
            result = await state.handle_cdp_command(client, payload)
            response: Dict[str, Any] = {
                "id": payload["id"],
                "result": {} if result is None else result,
            }
            if isinstance(payload.get("sessionId"), str):
                response["sessionId"] = payload["sessionId"]
        except (BridgeError, json.JSONDecodeError) as exc:
            request_id = payload.get("id") if isinstance(payload, dict) else None
            response = {
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }
            if isinstance(payload, dict) and isinstance(payload.get("sessionId"), str):
                response["sessionId"] = payload["sessionId"]
        await client.send(response)

    try:
        async for message in socket:
            if message.type != WSMsgType.TEXT:
                continue
            task = asyncio.create_task(process_message(message.data))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await state.release_client(client)
        LOGGER.info("CDP client %s disconnected", client.identifier)
    return socket


STATE_KEY: web.AppKey[BridgeState] = web.AppKey("bridge_state", BridgeState)


def create_app(state: Optional[BridgeState] = None) -> web.Application:
    app = web.Application()
    app[STATE_KEY] = state or BridgeState()
    app.router.add_get("/", status)
    app.router.add_get("/json/version", json_version)
    app.router.add_get("/json", json_list)
    app.router.add_get("/json/list", json_list)
    app.router.add_route("*", "/json/new", json_new)
    app.router.add_get("/json/activate/{target_id}", json_activate)
    app.router.add_get("/json/close/{target_id}", json_close)
    app.router.add_get("/extension", extension_socket)
    app.router.add_get("/devtools/{kind:browser|page}/{target_id}", cdp_socket)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    web.run_app(create_app(), host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
