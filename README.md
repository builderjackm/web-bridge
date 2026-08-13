# Web Bridge CLI

`webbridge` 是 Kimi Web Bridge 的纯命令行客户端，用命令替代手写 `curl` 和 JSON。它直接请求现有 daemon：

```text
webbridge CLI ──HTTP──> 127.0.0.1:10086/command ──> 现有浏览器扩展 ──> 页面
```

本项目只包含客户端，不再提供 daemon、浏览器扩展或 daemon 管理命令。使用前需先安装并启动 Kimi Web Bridge；官方说明见 [Web Bridge 中文页面](https://www.kimi.com/zh-cn/features/webbridge)。

## 安装

需要 Python 3.9+ 和 [`uv`](https://docs.astral.sh/uv/)。在项目目录安装用户级全局命令：

```bash
uv tool install .
webbridge --version
```

`uv tool install` 使用独立的用户级环境，不需要 `sudo`，也不会修改系统 Python。如果 shell 找不到命令：

```bash
uv tool update-shell
```

更新与卸载：

```bash
uv tool install --force .
uv tool uninstall webbridge
```

## 用法

```text
webbridge [--endpoint URL] [--timeout SECONDS] SESSION COMMAND [ARGS...]
```

一个任务始终使用同一个 `SESSION`。全局选项必须放在 `SESSION` 之前。

| 命令 | 参数 |
| --- | --- |
| `navigate` | `URL [--new-tab] [--group-title TITLE]` |
| `find_tab` | `URL [--active]` |
| `snapshot` | 无 |
| `click` | `SELECTOR` |
| `fill` | `SELECTOR VALUE` |
| `mouse_click` | `SELECTOR` |
| `evaluate` | `CODE` |
| `key_type` | `TEXT` |
| `send_keys` | `KEYS [--repeat 1-100]` |
| `cdp` | `METHOD [PARAMS_JSON]` |
| `screenshot` | `[--format png\|jpeg] [--quality 0-100] [--selector SELECTOR] [--path PATH]` |
| `network` | `start\|stop\|list\|detail [--filter FILTER] [--request-id ID]` |
| `upload` | `SELECTOR FILE [FILE ...]` |
| `save_as_pdf` | `[--paper-format FORMAT] [--landscape] [--scale N] [--no-print-background] [--path PATH]` |
| `list_tabs` | 无 |
| `close_tab` | 无 |
| `close_session` | 无 |

常用示例：

```bash
webbridge research navigate https://example.com --new-tab --group-title "资料调研"
webbridge research snapshot
webbridge research click @e42
webbridge research fill @e51 "中文内容"
webbridge research mouse_click @e42
webbridge research key_type "literal text"
webbridge research send_keys "Mod+A"
webbridge research evaluate 'JSON.stringify({url: location.href})'
webbridge research cdp Runtime.evaluate '{"expression":"document.title","returnByValue":true}'
webbridge research screenshot --format jpeg --quality 60
webbridge research save_as_pdf --paper-format a4 --path /tmp/page.pdf
webbridge research list_tabs
```

以 `-` 开头的文本参数前面使用 `--`：

```bash
webbridge research key_type -- -literal
```

默认 endpoint 是 `http://127.0.0.1:10086/command`。可通过全局参数或环境变量覆盖：

```bash
webbridge --endpoint http://127.0.0.1:10086/command research snapshot
WEBBRIDGE_URL=http://127.0.0.1:10086/command webbridge research snapshot
```

`webbridge --help` 内置完整使用手册。手册保持 Web Bridge 文案，将原始 HTTP 示例逐项改写为 CLI，并补充执行层支持的 `mouse_click`、`key_type` 和 `send_keys`。

## 直接用 uv 脚本运行

不安装全局命令也可以运行根目录的 PEP 723 脚本；它没有第三方 Python 依赖：

```bash
uv run webbridge.py --help
uv run webbridge.py my-task snapshot
```

## 开发与验证

```bash
uv run --with pytest pytest -q
uv build
```

测试覆盖 17 个 action 的参数转换、别名、UTF-8 JSON、HTTP 错误和帮助文档完整性。
