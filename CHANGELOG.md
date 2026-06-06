# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (breaking)

- **`E2EAutomationClient.execute_python` now returns the captured value, not a
  status dict.** The value is whatever the worker-side code assigns to
  `_result` (an explicit convention — there is intentionally **no** implicit
  "single expression / last expression" evaluation, so behavior never depends on
  code-string syntax such as a trailing `;`). No `_result` assigned → returns
  `None` (side-effect call). JSON-serializable values keep their type.
- **`execute_python` is strictly fail-fast** (a test must never proceed on a
  wrong value):
  - worker-side code exception → `WorkerCodeError` (carries the worker
    traceback / stdout / stderr);
  - non-serializable `_result` (e.g. a live `QgsVectorLayer`) →
    `NonSerializableResultError` (convert to plain data in the worker first);
  - confirm gate (worker not in trusted mode) → `ConfirmationRequiredError`.
  All subclass `AutomationClientError` and are exported from
  `qgis_puppeteer.client` and `pytest_qgis_puppeteer`. The previous
  `raise_on_error=` flag is **removed**.
- **New `execute_python_detailed(code) -> ExecResult`** for inspection: returns
  a typed `ExecResult` (`success` / `result_set` / `value` / `stdout` / `stderr`
  / `error` / `traceback` / `result_serializable` / `result_type` / …) and does
  **not** raise on worker code failure. Use it when you need stdout/stderr or
  want to branch on success yourself. Two methods, two stable return types — no
  polymorphic flag.
- **Core `qgis_execute_python` no longer stringifies the result.** The handler
  runs the code (`exec`, single mode) and returns the value `_result` holds:
  JSON-serializable values keep their type; non-serializable ones are kept out of
  `result` and reported via `result_serializable=False` + `result_repr` +
  `result_type` (NaN/Inf count as non-serializable). New `result_set` field marks
  whether the code captured a value. The MCP gateway response shape is unchanged
  (still JSON-formatted for Claude).

### Fixed

- **`wait_for_widget` / `Locator.snapshot()` no longer time out on a modeless
  dialog parented to the main window.** A top-level `QDialog` shown with
  `parent=mainWindow` appeared in **both** `snapshot_ui`'s `visible_dialogs`
  (it is a top-level widget) **and** the `main_window` subtree (it is a QObject
  child of the main window). The selector resolver then saw the *same* widget
  twice and strict mode reported `selector_ambiguous`, so the widget was treated
  as "not found" and the wait timed out. `snapshot_ui` now emits a **disjoint
  forest**: any widget reported as its own root (`active_modal` / each
  `visible_dialogs` entry / `main_window`) is excluded from every other root's
  subtree, so each widget appears exactly once (this also covers dialog-parented-
  to-dialog nesting). The snapshot payload is correspondingly smaller. A
  regression introduced when the strict-mode selector resolver was unified across
  the live tree and the snapshot path (ADR-0002 §8.1).
- **Live-tree resolution (`click` / `fill` / `check_actionability`) with
  `scope="any"` no longer falsely reports `selector_ambiguous`** for a parented
  top-level dialog. `_collect_candidates` walked overlapping roots (the main
  window and the dialog that is also its descendant) and collected the same
  widget twice; candidates are now de-duplicated by object identity.

Initial public release. The package is a Hub/Worker bridge between Claude
Desktop / pytest and a running QGIS, plus a pytest plugin that drives QGIS
end-to-end with a Playwright-style API.

### Added — Hub / Worker / MCP gateway

- **WebSocket Hub** routing tool calls between Clients (Claude / pytest) and
  Workers (QGIS plugin instances). Multi-instance addressable via
  `instance_id` (pid-based by default).
- **Worker** runtime with handler registry, command dispatch, structured
  errors (`INVALID_COMMAND` / `HANDLER_ERROR` / `INTERNAL_ERROR`), and
  graceful shutdown.
- **MCP gateway** exposing the same toolset to Claude Desktop via the
  Model Context Protocol.
- **`qgis_puppet` QGIS plugin** that registers the host QGIS as a Worker on
  load, handles Qt lifecycle (event-loop integration, graceful shutdown),
  and auto-spawns the Hub if it's not already running.

### Added — QGIS tools (handler suite)

- **Layer tools**: `qgis_list_layers`, `qgis_get_layer_info`,
  `qgis_select_features`, `qgis_get_selected_features`.
- **Python executor** with 3-tier permission model
  (`qgis_execute_python`, `qgis_execute_with_permission`,
  `qgis_get_whitelist`, `qgis_clear_session_permissions`).
- **Screenshot & canvas**: `qgis_screenshot`, `qgis_get_canvas_extent`,
  `qgis_set_canvas_extent`.
- **UI tools**: `qgis_snapshot_ui`, `qgis_click_widget`,
  `qgis_set_widget_value`, `qgis_check_actionability`.
- **Dialog handlers**: `qgis_register_dialog_handler` /
  `qgis_unregister_dialog_handler` / `qgis_list_dialog_handlers` /
  `qgis_clear_dialog_handlers` — auto-respond to non-modal popups during a
  test.
- **Uncaught exception recorder**: `qgis_get_recent_exceptions` /
  `qgis_clear_recent_exceptions`. Installs a chained `sys.excepthook`
  ring buffer (maxlen=200) on plugin init so Qt slot exceptions that used
  to be swallowed at the C++ → Python boundary are now observable.
- **`test.*` namespace handlers** (gated by
  `QPUPPETEER_ALLOW_TEST_HANDLERS=1`): `test.signal_spy_start` /
  `test.signal_spy_get_emissions` / `test.signal_spy_count` /
  `test.signal_spy_stop` / `test.wait_for_signal` — Playwright-style API
  for asserting on Qt signal emissions from tests.

### Added — Selector / Locator / web-first assertions (pytest plugin)

- **Playwright-style `Locator` API** with auto-wait (existence /
  visibility / enabled / not-covered / editable) and **strict mode**
  (multi-match without `index` raises `SelectorAmbiguousError`).
- **Locator chain** (`parent.locator(child)`) for nested scope resolution.
- **Web-first assertions**: `expect(locator).to_have_text(...)`,
  `to_be_visible()`, etc., with built-in retry.
- **Selector keys**: `class`, `object_name`, `text`, `text_re` (regex),
  `text_contains` (substring, handy for Qt mnemonics like `保存(&S)`),
  `attr` (forward-compat dict-of-attributes), `title`, `label`
  (`QLabel.buddy()`), `placeholder` (`placeholderText()`), `role`
  (`getByRole` via `QAccessibleInterface`), `index`. Live tree (Worker)
  and snapshot (test side) share the same matcher.
- **Selector scopes**: `top_level`, `modal` (`activeModalWidget`),
  `modal_stack[N]` (nested-modal walk; `0` = outermost, `-1` = topmost),
  `main_window`, `widget:<id>`.
- **Auto-wait `stable` check**: `Locator(stable=True)` requires two
  consecutive `check_actionability` responses with the same `geometry`
  before progressing — absorbs animations (fade-in, slide-down) so a
  click doesn't fire on a moving widget.

### Added — Test environments (ADR-0003)

- **`environments.toml` partition model** — define environments with
  `qgis_bin` / `qgis_args` / `qgis_command` / `env`, tag tests with
  `@pytest.mark.qgis_env(<name>)`, select with `pytest --env=<name>`.
  4-tier precedence: CLI > env vars > `environments.toml` env spec > ini.
  Strategies for the no-env case: `default_environment="named"` +
  `default_name=...`, or `fail`.
- **`--env=all` / `--env=A,B` meta-parent mode** — run multiple
  environments sequentially in one `pytest` invocation. The parent
  deselects all items and dispatches a child `pytest.main()` per env via
  `pytest_sessionfinish(tryfirst=True)`. Aggregation: any `1` → `1`,
  hard errors (`2`/`3`/`4`) propagate immediately and abort remaining
  envs, single no-tests-collected (`5`) is absorbed but **all-`5`** is
  preserved as a guard against silent CI greens.
- **`extends` field** for environment inheritance: `qgis_args` is
  concatenated, `env` is shallow-merged with child winning, scalar
  fields are taken from base unless overridden. Forward-references and
  cycles raise `ValueError` at TOML load.
- **`--list-envs` CLI option** prints all environments as TSV.
- **`--env-strict` CLI option** — treat per-env "no tests collected"
  (exit 5) as a real failure when meta-parent dispatching.
- **`outputs/<env_name>/_env_stats.json`** per child run (schema v1) and
  **`outputs/summary.json`** aggregated across `--env=all` runs (totals
  + per-env stats + parent exit code).
- **JUnit env-prefix post-processing** — every
  `<testcase classname="X.Y">` is rewritten to `<env_name>::X.Y` at
  child session finish (idempotent; uses stdlib
  `xml.etree.ElementTree`). Lets CI tools merge per-env JUnit files
  into a single report without losing test identity.
- **`--maxfail` accumulation across envs** — `pytest --env=all
  --maxfail=N` now stops after a total of N failures, not N per env.
  Parent reads each child's `_env_stats.json`, tracks
  `cumulative_fails`, and injects `--maxfail=<remaining>` into the next
  child via `inject_maxfail()`.

### Added — Inline QGIS spawn helper (ADR-0004)

- **`spawn_qgis()` context manager** for tests where QGIS startup options
  *are* the test subject. Exposes `SpawnedWorker` (with pid-based
  `instance_id` resolution) and `WorkerRegisterTimeout`. Captures
  stdout/stderr via PIPE + drain threads (prevents Hub-deadlock on
  chatty QGIS log output), enforces argument exclusivity with
  `ValueError`, performs a dangling-worker last-resort
  `QgsApplication.exitQgis()` on Hub-still-registered cases, and skips
  cleanly under dev mode (`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`).
- **`spawn_qgis_fixture(*, scope, ...)` factory** — wrap `spawn_qgis()`
  in any pytest scope (function / class / module / session) via a
  single factory call in `conftest.py`. Supports `yield_client=True`
  for an `automation_client` (with `use_instance(...)` already wired)
  or `yield_client=False` (default) for a raw `SpawnedWorker`.
- **`@pytest.mark.fresh_qgis(args=[...], env={...})`** — spawn a
  brand-new QGIS for a single test without disturbing the session
  worker. Composes correctly with `qgis_env`.
- **`E2EAutomationClient.use_instance(instance_id)` /
  `get_default_instance()`** — set or query a sticky default instance
  for `call(...)` routing. Used internally by `fresh_qgis`.
- **Auto fresh_qgis after failure** (opt-in,
  `qgis_auto_fresh_after_failure = true`) — chained-failure isolation.
  When a test fails in `call`, `pytest_runtest_setup` automatically
  marks the next test with `@pytest.mark.fresh_qgis`.

### Added — Diagnostic bundle (failure forensics)

- **`actions.jsonl` action timeline** — every
  `E2EAutomationClient.call(...)` records a structured `ActionRecord`
  (timestamp, command, params, result/error, instance, duration_ms,
  step_path, screenshot_path; schema_version=2). On failure the
  timeline is written as one-record-per-line JSONL into the
  diagnostic bundle. Memory-bounded by `max_actions=5000` with
  drop-oldest, thread-safe `actions` list, and thread-local step
  stack.
- **`E2EAutomationClient.step(name)` context manager** — group
  actions into nested logical steps; each `ActionRecord` carries the
  current `step_path: tuple[str, ...]`.
- **Per-action screenshots** — each `call(...)` captures a screenshot
  *after* the action and stores `screenshot_path` on the
  corresponding record. PNGs accumulate under
  `<diag_root>/_pending/<test>/`; on failure the dir is renamed
  atomically to `<bundle>/screenshots/`, on success the autouse
  fixture deletes it. Opt-out:
  `qgis_diag_capture_screenshots = false`.
- **`spawn_stdout.log` / `spawn_stderr.log`** — captured QGIS
  subprocess output for `fresh_qgis` / `spawn_qgis()` failures.
- **`meta.json`** (nodeid, env name, qgis_bin/args/command resolution,
  exception type+message, instances list, schema_version=1),
  **`traceback.txt`**, and **`hub_stdout.log` / `hub_stderr.log`**
  (last 500 lines drained from the Hub subprocess).
- **`uncaught_exceptions.json`** — Qt/Python uncaught exceptions
  recorded during the failed test, dumped from the Worker's
  excepthook ring buffer.

### Added — Failure linkage

- **Uncaught Qt/Python exception → test failure**: a
  `pytest_runtest_call` hookwrapper clears the Worker's exception
  buffer before each call phase, and `pytest_runtest_makereport`
  queries it after. If any exception is found, a previously-passing
  test is promoted to **failed** (longrepr shows the exceptions); a
  previously-failed test gets the exceptions appended via
  `report.sections`. Opt-out:
  `qgis_fail_on_uncaught_exception = false`. Default ON because
  catching silent failures is the basic guarantee. Dev mode
  (`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`) skips the check so
  pre-existing exceptions from a long-running QGIS don't leak in.

### Documentation

- **ADR-0001**: qgis-puppeteer architecture (Hub / Worker / Client /
  MCP gateway, message protocol, instance routing).
- **ADR-0002**: E2E test architecture (Locator, web-first assertions,
  selector model, dialog handlers, auto-wait, fresh_qgis,
  diagnostic bundle, action timeline, uncaught exception linkage).
- **ADR-0003**: Test environments (partition model, precedence,
  meta-parent dispatch, summary.json aggregation, env-strict /
  extends / list-envs).
- **ADR-0004**: Inline QGIS spawn helper (`spawn_qgis()` context
  manager, `spawn_qgis_fixture` factory).
- User guide (`docs/user-guide.md`) with quickstarts (Claude Desktop
  / pytest / Python script), selector reference, dialog handler
  usage, env / fresh_qgis / spawn_qgis / diagnostic bundle sections,
  and troubleshooting.
- README architecture diagram and root README quickstart.
- Roadmap (`docs/roadmap.md`) with planned future work organized by
  effect category.

### CI

- GitHub Actions workflow (`.github/workflows/ci.yml`) runs tests on
  Ubuntu + Windows × Python 3.10 / 3.11 / 3.12, plus `ruff check` and
  `ruff format --check` as hard gates.

### Tests

- ~430 unit and pytester-based integration tests across the three
  packages, covering protocol message round-trips, selector matcher,
  Locator auto-wait, dialog handler dispatch, env partition
  resolution, meta-parent dispatch, spawn_qgis lifecycle, action
  recorder, exception recorder, and signal spy.

[0.1.0]: https://github.com/oruharo/qgis-puppeteer/releases/tag/v0.1.0
