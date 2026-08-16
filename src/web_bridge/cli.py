"""Manage the local WebBridge daemon."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
from urllib.error import URLError
from urllib.request import urlopen

from . import __version__

STATUS_URL = "http://127.0.0.1:9222/"
STATE_DIR = Path.home() / ".webbridge"
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"
START_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
WINDOWS_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _status_payload(timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    try:
        with urlopen(STATUS_URL, timeout=timeout) as response:  # noqa: S310
            payload = json.load(response)
    except (OSError, TimeoutError, URLError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("name") != "webbridge":
        return None
    return payload


def _write_pid(pid: int) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    PID_FILE.write_text(f"{pid}\n", encoding="utf-8")


def _read_pid() -> Optional[int]:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _remove_pid() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_synchronize = 0x00100000
    wait_object_0 = 0x00000000
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD

    handle = open_process(process_synchronize, False, pid)
    if not handle:
        # Access denied means the process exists but cannot be inspected. An
        # invalid PID is the normal Windows result for an exited process.
        return ctypes.get_last_error() != error_invalid_parameter
    try:
        return wait_for_single_object(handle, 0) != wait_object_0
    finally:
        close_handle(handle)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_alive(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _daemon_executable() -> str:
    if os.name != "nt":
        return sys.executable

    # A virtual-environment python.exe can start the base console interpreter in
    # a second process, which bypasses CREATE_NO_WINDOW and opens Windows Terminal.
    # The sibling pythonw.exe uses the windowless subsystem for both processes.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw) if pythonw.is_file() else sys.executable


def _spawn_daemon() -> subprocess.Popen[bytes]:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOG_FILE.open("ab") as log:
        kwargs: Dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = WINDOWS_CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(
            [_daemon_executable(), "-m", "web_bridge.daemon"],
            **kwargs,
        )


def start() -> int:
    current = _status_payload()
    if current:
        pid = current.get("pid")
        if isinstance(pid, int):
            _write_pid(pid)
        print(f"WebBridge daemon is already running (pid {pid or 'unknown'}).")
        return 0

    process = _spawn_daemon()
    _write_pid(process.pid)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        current = _status_payload(timeout=0.25)
        if current:
            daemon_pid = current.get("pid")
            if isinstance(daemon_pid, int):
                _write_pid(daemon_pid)
            else:
                daemon_pid = process.pid
            print(f"WebBridge daemon started (pid {daemon_pid}).")
            if not current.get("extensionConnected"):
                print("Browser extension is not connected yet.")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.05)

    if process.poll() is None:
        process.terminate()
    _remove_pid()
    print(f"Failed to start WebBridge daemon. See {LOG_FILE}.", file=sys.stderr)
    return 1


def stop() -> int:
    current = _status_payload()
    if not current:
        recorded_pid = _read_pid()
        if recorded_pid is None or not _pid_alive(recorded_pid):
            _remove_pid()
            print("WebBridge daemon is not running.")
            return 0
        print(
            "WebBridge daemon is not responding; refusing to stop an unverified PID.",
            file=sys.stderr,
        )
        return 1

    pid = current.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        print("WebBridge daemon returned an invalid PID.", file=sys.stderr)
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid()
        print("WebBridge daemon is not running.")
        return 0

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            _remove_pid()
            print("WebBridge daemon stopped.")
            return 0
        time.sleep(0.05)

    if hasattr(signal, "SIGKILL"):
        os.kill(pid, signal.SIGKILL)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.05)
    if _pid_alive(pid):
        print(f"Failed to stop WebBridge daemon (pid {pid}).", file=sys.stderr)
        return 1
    _remove_pid()
    print("WebBridge daemon stopped.")
    return 0


def restart() -> int:
    result = stop()
    return start() if result == 0 else result


def status() -> int:
    current = _status_payload()
    if not current:
        print("Daemon: stopped")
        return 1
    print(f"Daemon: running (pid {current.get('pid', 'unknown')}, {STATUS_URL})")
    extension = "connected" if current.get("extensionConnected") else "disconnected"
    print(f"Extension: {extension}")
    print(f"CDP clients: {current.get('cdpClients', 0)}")
    print(f"Page targets: {current.get('pageTargets', current.get('targets', 0))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webbridge", description="Manage the local WebBridge CDP daemon."
    )
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("start", help="Start the daemon in the background.")
    subcommands.add_parser("restart", help="Restart the daemon.")
    subcommands.add_parser("stop", help="Stop the daemon.")
    subcommands.add_parser("status", help="Show daemon and extension status.")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {
        "start": start,
        "restart": restart,
        "stop": stop,
        "status": status,
    }
    return commands[args.command]()


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
