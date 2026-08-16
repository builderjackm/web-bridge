# WebBridge CDP

WebBridge CDP 把用户当前的 Chrome 暴露为一个本机标准 CDP endpoint。项目只包含两个组件：

```text
CDP client ──HTTP/WebSocket──> 127.0.0.1:9333 daemon
                                      ▲
                                      │ WebSocket（扩展主动连接）
                                      │
                                Chrome Extension
                                      │
                                chrome.debugger API
```

不需要给 Chrome 增加远程调试启动参数，也不需要启用 Chrome 的远程调试端口。CDP client 只连接 daemon；daemon 负责标准 CDP discovery、browser session 和 page session，任意页面级 CDP method/event 通过扩展透明转发。

## 安装

需要 [`uv`](https://docs.astral.sh/uv/) 和 Chrome。项目要求 Python 3.9+；如果本机没有合适的 Python，`uv` 会自动准备。

### 1. 全局安装 CLI

在项目根目录执行：

```bash
uv tool install .
webbridge --version
```

`uv` 会把 `webbridge` 安装到独立环境，并把命令链接到用户级可执行目录。安装后可以在任意目录直接运行 `webbridge`。

如果终端提示找不到 `webbridge`，执行：

```bash
uv tool update-shell
```

然后重新打开终端。可用 `uv tool dir --bin` 查看全局命令目录。

本地代码更新后，在项目根目录重新执行安装即可替换旧版本：

```bash
uv tool install .
```

卸载：

```bash
uv tool uninstall webbridge
```

### 2. 安装 Chrome 扩展

1. 打开 `chrome://extensions`。
2. 开启「开发者模式」。
3. 点击「加载已解压的扩展程序」。
4. 选择本项目的 `extension` 目录。

CLI 和扩展需要分别安装；`uv tool install .` 不会自动修改 Chrome。

## 运行

启动并检查 daemon：

```bash
webbridge start
webbridge status
```

全部生命周期命令：

```bash
webbridge start
webbridge restart
webbridge stop
webbridge status
```

daemon 会在后台运行，固定监听 `127.0.0.1:9333`，不会暴露到局域网。日志位于 `~/.webbridge/daemon.log`。

扩展会自动连接 daemon。扩展图标显示 `ON` 表示连接成功；点击扩展图标可查看 Daemon、浏览器连接、CDP 客户端和可调试页面数量，也可访问 `http://127.0.0.1:9333/` 查看原始状态。CDP client 开始使用某个标签页后，Chrome 会显示该标签页正在被调试的提示，不需要额外点击授权。

## 使用 CDP

任何接受 CDP port 或 URL 的程序都可以直接连接 `9333`：

```bash
agent-browser --cdp 9333 open https://example.com
agent-browser --cdp 9333 snapshot
```

可用的标准入口：

- `http://127.0.0.1:9333/json/version`
- `http://127.0.0.1:9333/json/list`
- `ws://127.0.0.1:9333/devtools/browser/webbridge`
- `/json/list` 返回的 `ws://.../devtools/page/<targetId>`

daemon 支持 `Target.setAutoAttach`、target 创建/关闭/激活、扁平 session 路由以及 browser/page CDP WebSocket。扩展断开时，已有 CDP 连接会立即关闭；扩展会持续自动重连。

## 开发验证

```bash
uv sync
uv run webbridge --help
uv run --with pytest pytest -q
uv build
```

测试覆盖 discovery、browser CDP session、page CDP session、任意命令响应和 CDP event 回传。真实验收命令是：

```bash
agent-browser --cdp 9333 open https://example.com
agent-browser --cdp 9333 get title
agent-browser --cdp 9333 snapshot
```
