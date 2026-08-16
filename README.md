# WebBridge CDP

WebBridge exposes the user's current Chrome browser as a standard local CDP endpoint. The runtime has two components:

```text
CDP client ──HTTP/WebSocket──> 127.0.0.1:9222 daemon
                                      ▲
                                      │ WebSocket (extension-initiated)
                                      │
                                Chrome Extension
                                      │
                                chrome.debugger API
```

Chrome does not need remote-debugging launch arguments or an exposed remote-debugging port. CDP clients connect only to the daemon. The daemon provides standard CDP discovery, browser sessions, and page sessions, while the extension transparently forwards page-level CDP methods and events.

## Installation

WebBridge requires [`uv`](https://docs.astral.sh/uv/) and Chrome. The Python package requires Python 3.9 or newer; `uv` can provision a compatible Python version when needed.

### 1. Install the CLI globally

Run this command from the project root:

```bash
uv tool install .
webbridge --version
```

`uv` installs WebBridge in an isolated environment and links the command into the user executable directory. The `webbridge` command can then be used from any directory.

If the shell cannot find `webbridge`, run:

```bash
uv tool update-shell
```

Restart the terminal afterward. Run `uv tool dir --bin` to show the global executable directory.

To replace an existing installation after updating the local source, run the installation command again from the project root:

```bash
uv tool install .
```

To uninstall the CLI:

```bash
uv tool uninstall webbridge
```

### 2. Install the Chrome extension

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the `extension` directory from this repository.

The CLI and extension are installed separately. `uv tool install .` does not modify Chrome.

## Running WebBridge

Start the daemon and inspect its status:

```bash
webbridge start
webbridge status
```

Available lifecycle commands:

```bash
webbridge start
webbridge restart
webbridge stop
webbridge status
```

The daemon runs in the background and listens only on `127.0.0.1:9222`; it is not exposed to the local network. On Windows, it starts without opening a console window. Logs are written to `~/.webbridge/daemon.log`.

The extension connects to the daemon automatically. An `ON` badge indicates a successful connection. Click the extension icon to view daemon status, browser connection status, CDP client count, and debuggable page count. The raw status is also available at `http://127.0.0.1:9222/`.

Chrome displays its standard debugging banner after a CDP client starts using a tab. No additional authorization click is required.

## Connecting CDP clients

Any program that accepts a CDP port or URL can connect directly to port `9222`:

```bash
agent-browser --cdp 9222 open https://example.com
agent-browser --cdp 9222 snapshot
```

Standard endpoints:

- `http://127.0.0.1:9222/json/version`
- `http://127.0.0.1:9222/json/list`
- `ws://127.0.0.1:9222/devtools/browser/webbridge`
- The `ws://.../devtools/page/<targetId>` URLs returned by `/json/list`

The daemon supports `Target.setAutoAttach`, target creation, closing and activation, flattened session routing, and browser/page CDP WebSockets. Existing CDP connections close immediately when the extension disconnects, and the extension keeps reconnecting automatically.

## Development and validation

```bash
uv sync
uv run webbridge --help
uv run --with pytest pytest -q
uv build
```

Tests cover discovery, browser CDP sessions, page CDP sessions, arbitrary command responses, and CDP event forwarding. Real-browser acceptance commands:

```bash
agent-browser --cdp 9222 open https://example.com
agent-browser --cdp 9222 get title
agent-browser --cdp 9222 snapshot
```
