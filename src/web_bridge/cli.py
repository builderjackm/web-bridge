"""Command-line wrapper around the local Web Bridge daemon."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from importlib.resources import files
from typing import Any, NoReturn, Optional, Sequence

from . import __version__


DEFAULT_ENDPOINT = "http://127.0.0.1:10086/command"
DEFAULT_TIMEOUT = 60.0


class CliError(Exception):
    """An error that can be shown directly to the CLI user."""


def fail(message: str, exit_code: int = 1) -> NoReturn:
    print(f"错误: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def load_help_document() -> str:
    return files("web_bridge").joinpath("help.md").read_text(encoding="utf-8").strip()


def json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"不是有效 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("必须是 JSON 对象")
    return parsed


def quality(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是 0 到 100 的整数") from exc
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("必须是 0 到 100 的整数")
    return parsed


def scale(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是 0.1 到 2.0 的数字") from exc
    if not 0.1 <= parsed <= 2.0:
        raise argparse.ArgumentTypeError("必须是 0.1 到 2.0 的数字")
    return parsed


def repeat_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是 1 到 100 的整数") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("必须是 1 到 100 的整数")
    return parsed


def add_action_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    *,
    aliases: Sequence[str] = (),
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, aliases=list(aliases), help=help_text)
    parser.set_defaults(action=name)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webbridge",
        description=(
            "通过 Web Bridge 操作当前浏览器；"
            "格式：webbridge <session-id> <command>。"
        ),
        epilog=load_help_document(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("WEBBRIDGE_URL", DEFAULT_ENDPOINT),
        help=f"daemon command URL（默认：{DEFAULT_ENDPOINT}）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"请求超时秒数（默认：{DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "session",
        metavar="session-id",
        help="当前任务固定使用的 WebBridge session ID",
    )

    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    navigate = add_action_parser(
        commands, "navigate", help_text="打开 URL 或导航当前标签"
    )
    navigate.add_argument("url")
    navigate.add_argument("--new-tab", action="store_true", help="新建标签页")
    navigate.add_argument("--group-title", help="首次导航时设置标签组标题")

    find_tab = add_action_parser(
        commands,
        "find_tab",
        aliases=("find-tab",),
        help_text="按 URL 选择标签页",
    )
    find_tab.add_argument("url")
    find_tab.add_argument("--active", action="store_true", help="借用用户当前标签页")

    add_action_parser(commands, "snapshot", help_text="读取当前页无障碍树")

    click = add_action_parser(commands, "click", help_text="点击 @e 引用或 CSS 选择器")
    click.add_argument("selector")

    fill = add_action_parser(commands, "fill", help_text="填写输入框或富文本编辑器")
    fill.add_argument("selector")
    fill.add_argument("value")

    mouse_click = add_action_parser(
        commands,
        "mouse_click",
        aliases=("mouse-click",),
        help_text="通过 CDP 发送真实鼠标点击",
    )
    mouse_click.add_argument("selector")

    evaluate = add_action_parser(commands, "evaluate", help_text="在当前页执行 JavaScript")
    evaluate.add_argument("code")

    key_type = add_action_parser(
        commands,
        "key_type",
        aliases=("key-type",),
        help_text="通过 CDP 向当前焦点输入文本",
    )
    key_type.add_argument("text")

    send_keys = add_action_parser(
        commands,
        "send_keys",
        aliases=("send-keys",),
        help_text="发送按键或组合键",
    )
    send_keys.add_argument("keys")
    send_keys.add_argument("--repeat", type=repeat_count)

    cdp = add_action_parser(commands, "cdp", help_text="调用原始 Chrome DevTools Protocol")
    cdp.add_argument("method")
    cdp.add_argument("params", nargs="?", type=json_object, default={}, help="JSON 对象")

    screenshot = add_action_parser(commands, "screenshot", help_text="截取当前页或指定元素")
    screenshot.add_argument("--format", choices=("png", "jpeg"))
    screenshot.add_argument("--quality", type=quality)
    screenshot.add_argument("--selector")
    screenshot.add_argument("--path")

    network = add_action_parser(commands, "network", help_text="记录或查询网络请求")
    network.add_argument("cmd", choices=("start", "stop", "list", "detail"))
    network.add_argument("--filter")
    network.add_argument("--request-id")

    upload = add_action_parser(commands, "upload", help_text="向文件输入框上传文件")
    upload.add_argument("selector")
    upload.add_argument("files", nargs="+")

    pdf = add_action_parser(
        commands,
        "save_as_pdf",
        aliases=("save-as-pdf",),
        help_text="把当前页保存为 PDF",
    )
    pdf.add_argument(
        "--paper-format",
        choices=("letter", "a4", "legal", "a3", "tabloid"),
    )
    pdf.add_argument("--landscape", action="store_true")
    pdf.add_argument("--scale", type=scale)
    pdf.add_argument(
        "--no-print-background",
        action="store_true",
        help="不打印页面背景",
    )
    pdf.add_argument("--path")

    add_action_parser(
        commands,
        "list_tabs",
        aliases=("list-tabs",),
        help_text="列出当前 session 的标签页",
    )
    add_action_parser(
        commands,
        "close_tab",
        aliases=("close-tab",),
        help_text="关闭当前 session 的当前标签页",
    )
    add_action_parser(
        commands,
        "close_session",
        aliases=("close-session",),
        help_text="关闭当前 session 的全部标签页",
    )
    return parser


def optional_args(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def command_args(args: argparse.Namespace) -> dict[str, Any]:
    action = args.action
    if action == "navigate":
        payload: dict[str, Any] = {"url": args.url}
        if args.new_tab:
            payload["newTab"] = True
        if args.group_title is not None:
            payload["group_title"] = args.group_title
        return payload
    if action == "find_tab":
        payload = {"url": args.url}
        if args.active:
            payload["active"] = True
        return payload
    if action in {"snapshot", "list_tabs", "close_tab", "close_session"}:
        return {}
    if action == "click":
        return {"selector": args.selector}
    if action == "fill":
        return {"selector": args.selector, "value": args.value}
    if action == "mouse_click":
        return {"selector": args.selector}
    if action == "evaluate":
        return {"code": args.code}
    if action == "key_type":
        return {"text": args.text}
    if action == "send_keys":
        return optional_args(keys=args.keys, repeat=args.repeat)
    if action == "cdp":
        return {"method": args.method, "params": args.params}
    if action == "screenshot":
        return optional_args(
            format=args.format,
            quality=args.quality,
            selector=args.selector,
            path=args.path,
        )
    if action == "network":
        return optional_args(
            cmd=args.cmd,
            filter=args.filter,
            requestId=args.request_id,
        )
    if action == "upload":
        return {"selector": args.selector, "files": args.files}
    if action == "save_as_pdf":
        payload = optional_args(
            paper_format=args.paper_format,
            scale=args.scale,
            path=args.path,
        )
        if args.landscape:
            payload["landscape"] = True
        if args.no_print_background:
            payload["print_background"] = False
        return payload
    raise CliError(f"不支持的命令：{action}")


def build_request_payload(args: argparse.Namespace) -> dict[str, Any]:
    session = args.session.strip()
    if not session:
        raise CliError("session 不能为空")
    return {
        "action": args.action,
        "args": command_args(args),
        "session": session,
    }


def response_error(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return "服务端未返回错误详情"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:1000]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:1000]


def send_command(endpoint: str, payload: dict[str, Any], timeout: float) -> Any:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        detail = response_error(exc.read())
        raise CliError(f"WebBridge 请求失败（HTTP {exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise CliError(
            "无法连接 Web Bridge："
            f"{reason}。请确认 Web Bridge daemon 与浏览器扩展已经启动"
        ) from exc
    except TimeoutError as exc:
        raise CliError(f"WebBridge 请求在 {timeout:g} 秒后超时") from exc

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as exc:
        preview = response_body.decode("utf-8", "replace")[:500]
        raise CliError(f"WebBridge 返回了无效 JSON：{preview}") from exc


def main(argv: Optional[Sequence[str]] = None) -> None:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.timeout <= 0:
            raise CliError("timeout 必须大于 0")
        payload = build_request_payload(args)
        response = send_command(args.endpoint, payload, args.timeout)
    except CliError as exc:
        fail(str(exc))
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
