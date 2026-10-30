# qgis-puppeteer

Control QGIS from Claude Desktop, pytest, or any Python script via WebSocket.

[![PyPI](https://img.shields.io/pypi/v/qgis-puppeteer.svg)](https://pypi.org/project/qgis-puppeteer/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

## Features

- **Multi-instance** — multiple QGIS processes managed by a single Hub
- **MCP server** — first-class Claude Desktop integration via [Model Context Protocol](https://modelcontextprotocol.io/)
- **Direct API** — `AutomationClient` for scripts, pytest, etc.
- **Local-only by default** — `127.0.0.1` binding, Origin header validation
- **Auto-managed lifecycle** — Hub spawns on demand, terminates 30s after last client disconnects
- **Permission system** — confirm-before-execute for arbitrary Python code (bypassable via env for CI)

## Installation

```bash
pip install qgis-puppeteer
```

You also need:
- QGIS 3.34+
- The QGIS Puppeteer plugin (install via QGIS Plugin Manager, or download from [QGIS Plugin Repo](https://plugins.qgis.org/)).
  The plugin folder (`plugins/qgis_puppeteer/` in the repository) carries a copy of this package
  plus its own GPL `qgis_plugin/` part, so QGIS needs nothing
  from pip. Do not `pip install qgis-puppeteer` into QGIS's own Python: that copy would hide
  the plugin folder

## Usage with Claude Desktop

Install the MCP extra first — it pulls in the MCP SDK the gateway imports:

```bash
pip install "qgis-puppeteer[mcp]"
```

Edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "qgis-puppeteer": {
      "command": "python",
      "args": ["-m", "qgis_puppeteer.gateways.mcp"]
    }
  }
}
```

Now Claude has tools available like:
- `mcp__qgis-puppeteer__qgis_list_layers`
- `mcp__qgis-puppeteer__qgis_select_features`
- `mcp__qgis-puppeteer__qgis_screenshot`
- `mcp__qgis-puppeteer__qgis_use_instance` (for multi-QGIS)

## Direct Python usage

```python
from qgis_puppeteer import AutomationClient

async with AutomationClient() as client:
    layers = await client.call("qgis_list_layers")
    print(layers)
```

## Available commands

| Category | Commands |
|---|---|
| Layers | `qgis_list_layers`, `qgis_get_layer_info` |
| Canvas | `qgis_get_canvas_extent`, `qgis_set_canvas_extent`, `qgis_canvas_mouse`, `qgis_canvas_key` (mouse and key input the map tool receives) |
| Selection | `qgis_select_features`, `qgis_get_selected_features` |
| UI | `qgis_snapshot_ui`, `qgis_click_widget`, `qgis_set_widget_value`, `qgis_read_rows`, `qgis_close_widget`, `qgis_check_actionability` (whether a widget can be acted on now) |
| Dialogs | `qgis_register_dialog_handler`, `qgis_unregister_dialog_handler`, `qgis_list_dialog_handlers`, `qgis_clear_dialog_handlers` — answer modal dialogs automatically |
| Capture | `qgis_screenshot` (what the canvas shows), `qgis_render_map` (fixed extent / size / DPI, for image snapshots) |
| Code | `qgis_execute_python`, `qgis_execute_with_permission`, `qgis_get_whitelist`, `qgis_clear_session_permissions` |
| Discovery (MCP only) | `qgis_list_instances`, `qgis_use_instance`, `qgis_wait_ready` — from Python, use `list_instances()` / `wait_for_ready()` |
| Behind the screen | `qgis_get_recent_exceptions`, `qgis_get_recent_messages` (QgsMessageLog, incl. message bar), and their `qgis_clear_*`; `qgis_get_leftover_state` (open dialogs, layers in editing, … after a task) |
| Waiting (MCP only) | `qgis_wait_for` — until a widget is visible / enabled / has a text or value, or a modal dialog or popup menu opens or closes; from pytest, use `expect(locator)` and `wait_for_modal()` / `wait_for_popup()` |
| Recording (MCP only) | `qgis_record_start`, `qgis_record_step`, `qgis_record_stop` — the calls go to a JSONL file in the `actions.jsonl` shape |

For a description of each tool, see the [MCP tool list](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/user-guide.md#mcp-tools) in the user guide. For full schemas and examples, see [Architecture document](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md).

## Architecture

```
[your client]
     ↓ WebSocket
[Hub] ← one port on 127.0.0.1, picked by the OS and written to the Hub file
     ↓ WebSocket (reverse-connect)
[QGIS + QGIS Puppeteer plugin]
```

- The Hub spawns automatically when QGIS starts; clients connect to it
- The Hub writes its address, token and pid to the **Hub file**; QGIS and
  clients read that file each time they connect
- Multiple QGIS instances share the same Hub
- Hub idle-shutdowns 30s after last client disconnects

See [docs/architecture/0001-qgis-puppeteer-architecture.md](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md) for full design.

## Configuration via environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QPUPPETEER_HUB_FILE` | `%LOCALAPPDATA%\qgis_puppeteer\hub.json` (Windows), `$XDG_RUNTIME_DIR/qgis_puppeteer/hub.json` or `~/.local/state/qgis_puppeteer/hub.json` (POSIX) | The Hub file: where the Hub writes its address and token, and where QGIS and clients read them. When set, the QGIS plugin does not start a Hub itself — it waits for the Hub that writes this file |
| `QPUPPETEER_HUB_LOG_FILE` | `hub.spawn.log` next to the Hub file | Where the output of a Hub the QGIS plugin starts goes |
| `QPUPPETEER_WORKER_LABEL` | — | Self-reported label (auto-assigned if empty) |
| `QPUPPETEER_TRUSTED_MODE` | unset | If `1`, bypass `execute_python` confirm step (CI / E2E) |
| `QPUPPETEER_LOG_LEVEL` | `INFO` | Log level of the MCP gateway (`DEBUG` / `INFO` / `WARNING` / ...), for both its stderr and `gateway.log`. It does not change the Hub's or QGIS's logs |
| `QPUPPETEER_GATEWAY_LOG` | `<TEMP>/qgis_puppeteer/gateway.log` | The MCP gateway's log file |

## Extending with custom handlers

You can register your own handlers from a QGIS plugin without depending on `qgis-puppeteer`. See [Architecture §12](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md#12-handler-extension-mechanism).

```python
# your_qgis_plugin/puppeteer_api.py
def build_handlers(iface):
    return {
        "mydomain.do_something": lambda params: {"result": "ok"},
    }
```

The QGIS Puppeteer plugin discovers and registers it automatically.

## License

Apache-2.0. See [LICENSE](LICENSE).
