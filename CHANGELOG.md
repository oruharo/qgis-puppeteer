# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`E2EAutomationClient.wait_for_ready()`, and the E2E fixtures use it.** The
  liveness work went into the shared Hub/Worker/client layers, so pytest E2E
  already benefited — a long `qgis_execute_python` no longer demotes its own
  instance out of `list_instances` — but the readiness notion stopped at
  `wait_for_worker()`, which only checks that an active instance is listed.
  The MCP side measured 20s between "listed" and "all layers loaded". The E2E
  client now has the same two-step wait (state=active plus a cheap read-only
  round trip, optionally `require_project=True`), and `hub_ready` and
  `spawn_qgis()` go through it. Raise the `qgis_startup_timeout` ini (or
  `register_timeout_s`) for heavy projects.

- **The Qt integration tests run again, and cover the field-reported scenarios.**
  `test_worker_integration` and friends were skipped everywhere — no PyQt5 in
  the venv or in CI — and had not been executed since ADR-0005 (two assertions
  still expected the old `worker-<label>-<pid>` ids). They now run in CI on
  Linux (PyQt5 is a dev dependency there) and on Windows through
  `scripts/qt_tests.py`, which drives QGIS's own Python so the Hub subprocess
  uses a consistent Qt. New `test_worker_liveness_integration` reproduces what
  mapix verified by hand: Hub killed → Worker respawns it and re-registers;
  GUI stalled past the heartbeat → `unresponsive` stays listed and recovers;
  `update_project` → `project` set and routable by basename; drop without bye →
  `instance_disconnected` with `grace_expires_in`.

- **`qgis_wait_ready` MCP tool / `AutomationClient.wait_for_ready()`.** Waits until
  an instance can actually take a call: `state == "active"` *and* a cheap
  read-only round trip (`qgis_get_canvas_extent`) comes back, so a QGIS that is
  listed but still loading its project on the GUI thread is not reported ready.
  Times out with `not_ready` and a snapshot of every instance's state. Replaces
  the hand-written polling scripts each client had to carry. Ready is not the
  same as "project loaded": QGIS's GUI is briefly idle before it starts reading
  the project (measured: ready at 11s, all 39 layers at 31s), so
  `require_project=True` additionally waits for `project` to be set — which now
  happens from `iface.projectRead`, after the layers exist.
- **`instance_disconnected` error code.** A selector that matches a Worker in
  its reconnect grace period now gets this code, with `last_seen_ago` and
  `grace_expires_in`, instead of `instance_not_found`. "Wrong selector" and "it
  was here a moment ago" are different problems; conflating them cost real time
  retrying selectors that were correct.
- **`list_instances(include_disconnected=True)`.** Returns grace-period entries
  as `state="disconnected"` with `grace_expires_in`. The MCP `qgis_list_instances`
  uses it, so a QGIS that just died or dropped its connection is visible for 60s
  with a countdown rather than silently gone.

- **The dialog-handler tools are now on the MCP surface.**
  `qgis_register_dialog_handler`, `qgis_unregister_dialog_handler`,
  `qgis_list_dialog_handlers` and `qgis_clear_dialog_handlers` existed as worker
  commands and were documented in the user guide as MCP tools — with a "Claude
  Desktop からの利用例" showing Claude registering a handler — but the gateway never
  published them, so that flow could not happen. The gateway now exposes all four
  (20 tools in total), with `action` and `permission` narrowed to enums in the
  input schema.
- **Tool and parameter descriptions now document the contracts the model has to
  follow.** `qgis_execute_python` / `qgis_execute_with_permission` state that a value
  comes back only when the code assigns `_result`; `instance` and `selector` carry
  JSON-Schema descriptions (selector lists every matching and scoping key);
  `qgis_snapshot_ui`, `qgis_click_widget`, `qgis_set_widget_value`, `qgis_screenshot`
  and `qgis_select_features` spell out their defaults and the error codes they can
  return; and the server's `instructions` orient a client around instance selection,
  selectors, `_result` and `isError`. `qgis_use_instance`'s ADR rationale moved into a
  code comment — it was being shipped to the model as the tool description.
- **MCP tools now carry titles and annotations.** All 16 tools declare a display
  title and `ToolAnnotations`. The seven query-only tools (`qgis_list_instances`,
  `qgis_list_layers`, `qgis_get_layer_info`, `qgis_get_selected_features`,
  `qgis_get_whitelist`, `qgis_get_canvas_extent`, `qgis_snapshot_ui`) are marked
  `readOnlyHint`, which lets MCP clients dispatch them in parallel, while
  `qgis_execute_python`, `qgis_execute_with_permission`, `qgis_click_widget` and
  `qgis_set_widget_value` carry `openWorldHint` because one call can turn into
  arbitrary work. The server itself now reports a title, its package version and
  its project URL — under the 2026-07-28 revision that identity travels in every
  result's `_meta`, where the version was previously empty.

- **E2E coverage guide + ready-to-use templates** (`examples/coverage/`). A new
  user-guide section ("E2E カバレッジ計測") explains how to measure coverage of the
  code that runs *inside* QGIS — the two-process model means `pytest --cov` alone
  only covers the runner, not the plugin/app under test. It uses coverage.py
  subprocess measurement; since qgis-puppeteer already propagates the pytest
  process environment to the spawned QGIS, setting `COVERAGE_PROCESS_START` is
  enough to switch it on. Templates: a `.coveragerc`, a profile-scoped
  `startup.py` hook (recommended), and a `coverage_subprocess.pth` alternative.
  Deliberately *not* wired into the `qgis_puppet` plugin: a plugin-load-time hook
  fires too late to capture import-time lines and would silently under-report.

### Changed (breaking)

- **A selector that matches a Worker in its reconnect grace period now fails
  with `instance_disconnected` instead of `instance_not_found`.** Clients that
  retried on `instance_not_found` during a QGIS restart should treat
  `instance_disconnected` the same way (and can use its `grace_expires_in` to
  bound the wait).

- **`list_instances` now includes unresponsive Workers, with `state` and
  `last_seen_ago`.** Each `InstanceInfo` carries `state` (`"active"`,
  `"unresponsive"`, or `"disconnected"` when asked for) and `last_seen_ago`
  (seconds since the Hub last got a pong).
  Previously an instance that had missed heartbeats was silently dropped from the
  list. Clients that treated "listed" as "ready" should check `state`;
  `AutomationClient.wait_for_instance` / `wait_for_new_instance` and the pytest
  `wait_for_worker` already do, and keep waiting while the instance is
  unresponsive. Older clients decoding a new Hub's list see the extra keys and
  ignore them; a new client talking to an older Hub defaults `state` to
  `"active"`.

- **The MCP tool can no longer grant a permanent permission.**
  `qgis_execute_with_permission` used to accept `permission="always"`, which writes
  the code into `<project root>/.claude/qgis_whitelist.json` and then runs it without
  asking in every later session. That argument is filled in by the caller, and nothing
  verifies that a human was consulted, so an MCP client could widen the permanent
  policy on its own in a single call. The MCP tool now accepts `once`, `session` and
  `cancel` only; the worker command still takes `always` for the pytest and script
  paths, where the caller is the user's own code.

- **Failed tool calls are reported with `isError`.** `hub_unreachable`,
  worker-side `RequestError`s and `instance_not_found` used to come back as
  *successful* results whose body happened to contain an `error` object, so a
  client could not tell a failed call from a working one. They now return
  `isError: true`. The body is unchanged — the same readable JSON with the same
  error codes — because raising would have the client fold it into a generic
  "error executing tool" message.
- **Tool results no longer carry `structuredContent`.** The `-> str` return
  annotations made the SDK publish an auto-generated `{"result": "<string>"}`
  output schema and mirror the whole JSON body into `structuredContent`, so every
  result travelled twice (a 333-character error result carried 381 further
  characters of duplicate; UI snapshots are far larger). The contract is, and
  stays, the JSON text in the content block.
- **The `mcp` extra now requires `mcp>=2,<3`; the MCP gateway targets mcp 2.x.**
  mcp 2.x renamed `FastMCP` to `MCPServer` and moved it to
  `mcp.server.mcpserver`, so the gateway raised `ModuleNotFoundError: No module
  named 'mcp.server.fastmcp'` at import as soon as the old unbounded `mcp>=1.0`
  requirement resolved to 2.x — an MCP client saw only the connection close.
  `qgis_puppeteer.gateways.mcp` now imports `Context` / `MCPServer` from
  `mcp.server.mcpserver` and no longer imports under mcp 1.x, so install it with
  the extra (`pip install "qgis-puppeteer[mcp]"`). The requirement is capped at
  the major version so the next major cannot break the gateway the same way. The
  MCP tool surface and the response shapes are unchanged.
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

- **`spawn_qgis()` could return an instance that cannot answer.** Its
  launch_token wait matched on the token alone, and `list_instances` now also
  returns instances whose heartbeat has stopped, so a QGIS still busy loading
  was reported as spawned and the caller's first command blocked until the GUI
  thread freed up. `wait_for_worker()` gained the `state == "active"` filter
  when the state was introduced; this path did not. It now shares
  `wait_for_ready()` with the rest.

- **The gateway no longer leaks exceptions as a bare `Error executing tool`.**
  Right after a Hub swap, `qgis_list_instances` failed three times in a row with
  that one line and no JSON body, then recovered only after some other tool
  happened to run. Anything outside the stale-connection set escaped the tool,
  the MCP SDK treated it as a crash, and the caller saw neither the exception
  type nor its message — the gateway's stderr is not visible from the client.
  Every tool now goes through one path: a stale connection or an unexpected
  exception on the first attempt resets the connection and retries once; a
  second failure comes back as `gateway_internal_error` with the exception type
  and the tail of the traceback. A reconnect whose register is rejected is
  reported as `hub_unreachable` with the reason instead of escaping.

- **A Worker whose Hub died could never get it back.** The reconnect timer
  only reopened the socket; spawning the Hub (`ensure_hub_reachable`) ran once,
  in `connect_to_hub`, at plugin load. Kill the Hub and QGIS sat alive but
  isolated forever, cycling connect → refused → retry. The reconnect path now
  goes through `ensure_hub_reachable` too (a TCP probe when the Hub is up, a
  spawn when it is not), with exponential backoff up to 30s so a Hub that
  cannot be started does not keep stalling the GUI thread. Killing the Hub is
  now a supported way to upgrade it while QGIS keeps running.
- **`qgis_wait_ready` no longer fails on the first call after QGIS starts.**
  It told callers to call it first, but returned `hub_unreachable` immediately
  when the Hub was not listening yet — which is exactly the situation right
  after launch. It now retries the connection within `timeout_s` and, if the
  Hub never comes up, reports `not_ready` with the unreachable reason in
  `details.hub_unreachable`. Other tools still return `hub_unreachable` at
  once.

- **`InstanceInfo.project` was frozen at plugin load.** It was read once when
  the Worker was built in `initGui`, before any project is open, and never
  refreshed — so a QGIS started through the launcher reported `project: null`
  forever, and the project-basename selector tier could not match it. The
  plugin now sends a new `update_info` message (Worker → Hub) from
  `iface.projectRead` (after the load completes), `QgsProject.cleared` and
  `QgsProject.fileNameChanged` (save-as); a Worker that reconnects carries the
  current value in its register. `fileNameChanged` alone was the first attempt
  and does not fire when a project is opened — verified on QGIS 3.34.
- **`qgis_list_instances` / `qgis_use_instance` / `qgis_wait_ready` reconnect
  after a Hub restart.** Only worker commands had the stale-connection retry;
  the discovery tools used the cached client as-is, so after swapping the Hub
  "list works but use_instance fails" depended on which tool hit the dead
  socket first. All tools now share one reconnect path.

- **A QGIS whose GUI thread stalls no longer turns into an unreachable zombie.**
  The Worker's `QWebSocket` lives on the QGIS GUI thread, so a heavy project load
  or a long `qgis_execute_python` stops heartbeat pongs while the process and the
  TCP connection stay alive. The Hub treated 20s without a pong as a disconnect:
  the instance vanished from `list_instances` and every call failed with
  `instance_not_found` even though QGIS was fine. Worse, a grace-period entry
  ignored pongs (`mark_seen` was a no-op for it), so once the GUI freed up the
  instance did **not** come back; after 60s the Hub deleted the entry without
  closing the socket, and the Worker — still connected, never told — never
  reconnected. Heartbeat loss is now a separate `unresponsive` state: the entry
  stays listed and routable, calls queue until the GUI thread is free, and the
  next pong restores `active`. Only after `UNRESPONSIVE_CLOSE_SECONDS` (300s)
  does the Hub assume a half-open socket, close it itself, and let the normal
  grace path run — which also triggers the Worker's auto-reconnect if it was
  alive. Same-label restarts after kill -9 still supersede promptly through the
  existing liveness probe.

- **The user guide no longer promises a QGIS confirmation dialog that does not
  exist.** Four places described the `confirm` tier as "QGIS が確認ダイアログを出して
  ユーザーに尋ねる", including a troubleshooting entry for tests hanging while waiting
  on it. No such UI exists anywhere in the code, and that hang cannot happen:
  `qgis_execute_python` refuses the call and returns `requires_confirmation` plus a
  risk analysis in the same round trip. The guide now says what actually happens and
  who involves a human — the MCP client's own approval UI, or the pytest wrapper
  raising `ConfirmationRequiredError` — spells out which permission values each caller
  may choose, and points at `<project root>/.claude/qgis_whitelist.json` as the
  remaining way to grant a permanent allowance. The same false premise is corrected
  in ADR-0002 §9 (with a dated note — the trusted-mode decision itself still holds)
  and in the `ConfirmationRequiredError` / pytest-plugin docstrings.

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
