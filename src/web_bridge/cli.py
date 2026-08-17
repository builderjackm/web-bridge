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
from typing import Any, Dict, List, Optional, Sequence, Set
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


def _write_pids(pids: Sequence[int]) -> None:
    """Record every process a running daemon owns, launcher included.

    A Windows virtual environment spawns the base interpreter as a child, so a
    single daemon is two processes and stopping only one strands the other.
    """
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    unique = list(dict.fromkeys(pid for pid in pids if pid > 1))
    PID_FILE.write_text("".join(f"{pid}\n" for pid in unique), encoding="utf-8")


def _read_pids() -> List[int]:
    try:
        lines = PID_FILE.read_text(encoding="utf-8").split()
    except OSError:
        return []
    pids: List[int] = []
    for line in lines:
        try:
            pid = int(line)
        except ValueError:
            continue
        if pid > 1:
            pids.append(pid)
    return list(dict.fromkeys(pids))


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


def _windows_process_executable(pid: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x00001000
    max_path = 32768

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    query_image_name = kernel32.QueryFullProcessImageNameW
    query_image_name.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_image_name.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(max_path)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not query_image_name(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value or None
    finally:
        close_handle(handle)


def _process_executable(pid: int) -> Optional[str]:
    """Path of the binary a PID runs, or None when it cannot be read."""
    if os.name == "nt":
        return _windows_process_executable(pid)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pass
    output = _run_text(["ps", "-p", str(pid), "-o", "comm="])
    return output.strip() or None if output else None


def _normalized_path(path: Any) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _daemon_interpreters() -> Set[str]:
    """Interpreters a WebBridge daemon can legitimately be running under.

    A Windows virtual environment launches the base interpreter in a second
    process, so a daemon reports either the venv path or the base one.
    """
    names = ("python.exe", "pythonw.exe") if os.name == "nt" else ("python", "python3")
    roots = {Path(sys.executable), Path(getattr(sys, "_base_executable", sys.executable))}
    candidates = {*roots, *(root.with_name(name) for root in roots for name in names)}
    return {_normalized_path(path) for path in candidates if path.is_file()}


def _is_daemon_process(pid: int) -> bool:
    """Whether a live PID is one of our daemons rather than a recycled PID."""
    if not _pid_alive(pid):
        return False
    executable = _process_executable(pid)
    if executable is None:
        return False
    return _normalized_path(executable) in _daemon_interpreters()


def _run_text(command: Sequence[str]) -> Optional[str]:
    kwargs: Dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = WINDOWS_CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout


def _signal_process(pid: int, number: int) -> bool:
    """Signal a PID; False when it had already exited."""
    try:
        os.kill(pid, number)
    except OSError:
        # Windows raises a bare OSError, not ProcessLookupError, for a dead PID.
        return False
    return True


def _stop_process(pid: int) -> bool:
    """Terminate a PID and wait for it to go away."""
    if not _signal_process(pid, signal.SIGTERM):
        return not _pid_alive(pid)

    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)

    if hasattr(signal, "SIGKILL"):
        _signal_process(pid, signal.SIGKILL)
        for _ in range(20):
            if not _pid_alive(pid):
                return True
            time.sleep(0.05)
    return not _pid_alive(pid)


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
            recorded = _read_pids()
            _write_pids(recorded if pid in recorded else [pid])
        print(f"WebBridge daemon is already running (pid {pid or 'unknown'}).")
        return 0

    process = _spawn_daemon()
    _write_pids([process.pid])
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        current = _status_payload(timeout=0.25)
        if current:
            daemon_pid = current.get("pid")
            if isinstance(daemon_pid, int):
                _write_pids([process.pid, daemon_pid])
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
    targets: List[int] = []

    if current:
        pid = current.get("pid")
        if not isinstance(pid, int) or pid <= 1:
            print("WebBridge daemon returned an invalid PID.", file=sys.stderr)
            return 1
        targets.append(pid)

    recorded = [pid for pid in _read_pids() if pid not in targets and _pid_alive(pid)]
    # A daemon that stopped answering its port is still ours to stop, but only
    # once the PID still points at an interpreter rather than a recycled process.
    verified = [pid for pid in recorded if _is_daemon_process(pid)]
    if recorded and not verified and not current:
        print(
            "WebBridge daemon is not responding; refusing to stop an unverified PID.",
            file=sys.stderr,
        )
        return 1
    targets.extend(verified)

    if not targets:
        _remove_pid()
        print("WebBridge daemon is not running.")
        return 0

    stubborn = [pid for pid in targets if not _stop_process(pid)]
    if stubborn:
        listed = ", ".join(str(pid) for pid in stubborn)
        print(f"Failed to stop WebBridge daemon (pid {listed}).", file=sys.stderr)
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
