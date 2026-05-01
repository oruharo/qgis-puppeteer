# pytest-qgis-puppeteer

Pytest plugin for E2E testing QGIS via [qgis-puppeteer](https://github.com/oruharo/qgis-puppeteer/tree/main/packages/qgis-puppeteer).

[![PyPI](https://img.shields.io/pypi/v/pytest-qgis-puppeteer.svg)](https://pypi.org/project/pytest-qgis-puppeteer/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

## Features

- **Playwright-style Locator API** — `qgis.locator({...})` with auto-wait
- **Auto-wait** for actionability (existence / visibility / enabled / not covered / editable)
- **Web-first assertions** — `expect(locator).to_have_text(...)` with built-in retry
- **Strict mode selector** — multi-match without `index` raises `SelectorAmbiguousError`
- **Modal dialog support** — works inside `exec_()` event loops
- **Test environments** — partition tests by QGIS startup config via `environments.toml` + `@pytest.mark.qgis_env(...)` (ADR-0003)
- **Inline spawn helper** — `spawn_qgis()` context manager for tests where startup options *are* the test subject (ADR-0004)
- **Diagnostic bundles** on failure (screenshots, UI snapshots, instance list)
- **Multi-QGIS support** for parallel testing

## Installation

```bash
pip install pytest-qgis-puppeteer
```

You also need the QGIS-side `qgis_puppet` plugin and `qgis-puppeteer` core (auto-installed as dependency).

## Configuration

### Simple case — direct executable

```toml
# Your project's pyproject.toml
[tool.pytest.ini_options]
qgis_bin = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"
qgis_args = ["--profile=test"]
qgis_startup_timeout = 30
asyncio_mode = "auto"
```

`qgis_python` (the QGIS-bundled Python launcher used to spawn the Hub
subprocess) is auto-detected from `OSGEO4W_ROOT` or `sys.executable`'s sibling.
Set it explicitly only if auto-detection fails:

```toml
qgis_python = "C:/Program Files/QGIS 3.34/bin/python-qgis-ltr.bat"
```

### Custom launcher — host-specific spawn

If your app starts QGIS through a wrapper script that needs to set up
environment variables (host-specific config, DB connection info, etc.)
before QGIS launches, use `qgis_command` instead of `qgis_bin`:

```toml
[tool.pytest.ini_options]
qgis_command = ["scripts/launcher.bat", "--config", "e2e", "--feature", "auth"]
```

`qgis_command` is mutually exclusive with `qgis_bin` / `qgis_args`. The plugin
still injects `QPUPPETEER_HUB_PORT` / `QPUPPETEER_TRUSTED_MODE` /
`QPUPPETEER_ALLOW_TEST_HANDLERS` into the spawn env — your wrapper just needs
to inherit and forward them to QGIS.

The plugin is auto-loaded via `entry_points`; no manual `pytest_plugins` setup
needed.

Override via environment variables:
- `QPUPPETEER_QGIS_BIN` / `QPUPPETEER_QGIS_COMMAND`
- `QPUPPETEER_QGIS_PYTHON`
- `QPUPPETEER_E2E_USE_RUNNING_QGIS=1` — reuse existing QGIS instance (dev mode)

### Full control — fixture override

For maximum flexibility (multi-step setup, post-spawn assertions, custom
teardown), override the `qgis_process` fixture in your `conftest.py`:

```python
# your_project/test_e2e/conftest.py
import pytest

@pytest.fixture(scope="session")
def qgis_process(automation_client, hub_port, my_custom_setup):
    # ... spawn QGIS your way, yield, teardown ...
```

The other fixtures (`hub_process`, `automation_client`, `qgis`) continue to
work as-is.

## Test environments — partition different test groups by QGIS config

When different test groups need different QGIS startup configurations
(e.g., authentication on vs off, LTR vs latest, online vs offline), use
the **environments** feature ([ADR-0003](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0003-test-environments.md)).

Define environments in `environments.toml` at your project root (or under
`test_e2e/`):

```toml
[[environments]]
name = "smoke"
qgis_args = ["--profile=test"]

[[environments]]
name = "with_authentication"
qgis_args = ["--profile=auth_test"]
env = { MYAPP_AUTH_ENABLED = "1" }

[[environments]]
name = "qgis_lts"
qgis_bin = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"

[default_environment]
strategy = "named"
default_name = "smoke"
```

Tag tests with `@pytest.mark.qgis_env(<name>)` to assign them to an
environment. Tests are filtered by `--env=<name>`:

```python
import pytest

@pytest.mark.qgis_env("with_authentication")
def test_login_flow(qgis):
    ...

@pytest.mark.qgis_env("qgis_lts", "smoke")  # opt-in cartesian
def test_basic_open_project(qgis):
    ...
```

```bash
pytest --env=smoke              # only tests assigned to "smoke"
pytest --env=with_authentication  # only auth tests, run with auth env
pytest                            # default_name env (here: "smoke")
```

Phase 1 supports `--env=<single_name>`. `--env=all` and comma-separated
lists are Phase 2. See the ADR for the full design.

### Inline spawn — when startup options are the test subject

When the test's purpose is to verify the QGIS startup option itself
(e.g., does `--clean-canvas` actually clear state? does `--profile=X`
load the right plugins?), put the option directly in test code instead
of hiding it behind a fixture or `environments.toml`
([ADR-0004](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0004-inline-qgis-spawn-helper.md)):

```python
from pytest_qgis_puppeteer import spawn_qgis

QGIS_BIN = "C:/Program Files/QGIS 3.34/bin/qgis-bin.exe"

def test_clean_canvas_clears_layers(hub_port, automation_client):
    with spawn_qgis(
        hub_port=hub_port,
        automation_client=automation_client,
        qgis_bin=QGIS_BIN,
        args=["--clean-canvas"],
    ) as worker:
        n = automation_client.execute_python(
            "len(QgsProject.instance().mapLayers())",
            instance=worker.instance_id,
        )
        assert n == 0
```

Each `spawn_qgis(...)` block reuses the session's Hub but spawns a fresh
Worker. Multiple `spawn_qgis()` calls (or coexistence with the
`qgis_process` fixture) all register to the same Hub — switch between
them with `automation_client.use_instance(worker.instance_id)`.

## Minimal example

```python
def test_login(qgis):
    # Open project, modal appears
    qgis.execute_python(
        "QgsProject.instance().read('test_project.qgs')"
    )
    qgis.wait_for_modal(title_contains="Login")

    # Operate inside the modal (auto-wait built into Locator)
    qgis.locator({"object_name": "username", "scope": "modal"}).fill("user")
    qgis.locator({"object_name": "password", "scope": "modal"}).fill("pass")
    qgis.locator({"object_name": "login_btn", "scope": "modal"}).click()

    qgis.wait_for_modal_closed()

    assert qgis.locator({"object_name": "status"}).get_text() == "Logged in"
```

> The full guide (selector cheatsheet, dialog handler, multi-instance, troubleshooting) lives in [docs/user-guide.md](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/user-guide.md).

## Locator API

```python
btn = qgis.locator({"object_name": "save_btn", "scope": "modal"})
btn.click()                # auto-wait + click
btn.fill("hello")          # for input widgets (auto-waits for editable=True)
btn.select("option")       # ComboBox / Tab / List
btn.check()                # CheckBox / RadioButton → True
text = btn.get_text()
visible = btn.is_visible()
```

Selectors:
- `object_name`: Qt `objectName()`
- `text`: button/label/action text
- `class`: Python class name
- `title`: window title
- `label`: associated `QLabel.buddy()` text (Playwright `getByLabel`)
- `placeholder`: `placeholderText()` of LineEdit/TextEdit
- `scope`: `"modal"` (default) / `"active_window"` / `"any"`
- `root_object_name`: top-level filter
- `index`: pick N-th match (without it, multi-match raises `SelectorAmbiguousError`)

See [User Guide §Selector reference](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/user-guide.md#selector-リファレンス) for examples.

## Diagnostic bundles

On test failure, the plugin writes a diagnostic bundle to
`outputs/diagnostics/{test_name}_{timestamp}/`:

```
screenshot.png         - QGIS main window screenshot
snapshot_ui.json       - qgis_snapshot_ui output (UI tree)
list_instances.json    - registered Worker instances at failure time
```

When using `--env=<name>` (see "Test environments" above), bundles are
nested under the env name: `outputs/<env_name>/diagnostics/...`.

Configurable via `[tool.pytest.ini_options]`:
- `qgis_diag_dir`: output directory (default `outputs/diagnostics`)

> Action-level capture (`actions.jsonl`, per-action screenshots, hub log
> tail, etc.) is on the roadmap — see ADR-0002 §13.

## Architecture

- [ADR-0002 E2E Test Architecture](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0002-e2e-test-architecture.md) — Locator, auto-wait, expect, fixtures
- [ADR-0003 Test Environments](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0003-test-environments.md) — partition model for `qgis_env` marker
- [ADR-0004 Inline spawn helper](https://github.com/oruharo/qgis-puppeteer/blob/main/docs/architecture/0004-inline-qgis-spawn-helper.md) — `spawn_qgis()` context manager

## License

Apache-2.0. See [LICENSE](LICENSE).
