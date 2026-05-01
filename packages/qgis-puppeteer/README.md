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
- The `qgis_puppet` plugin (install via QGIS Plugin Manager, or download from [QGIS Plugin Repo](https://plugins.qgis.org/))

## Usage with Claude Desktop

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
| Canvas | `qgis_get_canvas_extent`, `qgis_set_canvas_extent` |
| Selection | `qgis_select_features`, `qgis_get_selected_features` |
| UI | `qgis_snapshot_ui`, `qgis_click_widget`, `qgis_set_widget_value`, `qgis_check_actionability` |
| Capture | `qgis_screenshot` |
| Code | `qgis_execute_python`, `qgis_execute_with_permission`, `qgis_get_whitelist`, `qgis_clear_session_permissions` |
| Discovery | `qgis_list_instances`, `qgis_use_instance` |

For full schemas and examples, see [Architecture document](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md).

## Architecture

```
[your client]
     ↓ WebSocket
[Hub] ← single port (default 9876, 127.0.0.1)
     ↓ WebSocket (reverse-connect)
[QGIS + qgis_puppet plugin]
```

- The Hub spawns automatically when QGIS starts; clients connect to it
- Multiple QGIS instances share the same Hub
- Hub idle-shutdowns 30s after last client disconnects

See [docs/architecture/0001-qgis-puppeteer-architecture.md](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md) for full design.

## Configuration via environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QPUPPETEER_HUB_HOST` | `127.0.0.1` | Hub bind / connect host |
| `QPUPPETEER_HUB_PORT` | `9876` | Hub listen / connect port |
| `QPUPPETEER_HUB_URL` | — | Override host:port via `ws://...` |
| `QPUPPETEER_HUB_ORIGIN` | `http://localhost` | Origin header for register |
| `QPUPPETEER_WORKER_LABEL` | — | Self-reported label (auto-assigned if empty) |
| `QPUPPETEER_TRUSTED_MODE` | unset | If `1`, bypass `execute_python` confirm step (CI / E2E) |
| `QPUPPETEER_LOG_LEVEL` | `INFO` | Logging level |

## Extending with custom handlers

You can register your own handlers from a QGIS plugin without depending on `qgis-puppeteer`. See [Architecture §12](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0001-qgis-puppeteer-architecture.md#12-handler-extension-mechanism).

```python
# your_qgis_plugin/puppeteer_api.py
def build_handlers(iface):
    return {
        "mydomain.do_something": lambda params: {"result": "ok"},
    }
```

`qgis_puppet` discovers and registers it automatically.

## License

Apache-2.0. See [LICENSE](LICENSE).
