# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`qgis_wait_for` MCP tool.** An MCP client had no way to wait: after a
  `wait=false` click it called `qgis_snapshot_ui` again and again to see
  whether a dialog had opened or a button had become enabled, each time
  reading the whole UI tree. The tool waits inside the gateway, checking every
  0.25 s, until a widget is visible / hidden / enabled / disabled / actionable
  / editable, a count of widgets, a text, a value, a check state or a row count
  is reached, or a modal dialog or popup menu opens or closes. It returns what
  it saw last, and on timeout fails with `code=wait_timeout` and that
  observation. The conditions mean the same as pytest's `expect(locator)` and
  `wait_for_modal()` / `wait_for_popup()`, which now share the judgment with
  it, so a recorded wait turns into the matching pytest line.

- **`qgis_check_actionability` is an MCP tool.** The pytest `Locator` reads
  whether a widget exists, is visible, enabled, not covered by a modal and
  editable through this QGIS command, but an MCP client could not, so an
  agent whose click failed had to guess why from a full UI snapshot. The
  tool reads it once, without waiting, and returns the same result as the
  command, including the number of matches and the candidates when the
  selector matches several widgets.

- **`Locator.count()`** (#41). `expect(locator).to_have_count(n)` waits
  until the count is `n`, so there was no way to read how many widgets match
  right now; tests read `diagnostics.match_count` from `check_actionability`,
  which breaks when the diagnostics change shape. `count()` counts once,
  without waiting, and returns 0 when nothing matches; several matches are not
  an ambiguity error. It counts as `to_have_count` does (visible widgets only,
  `index` ignored), and `to_have_count` now polls it.

- **The pytest client answers dialogs** (#40). Dialog handlers had MCP
  tools but no client methods, so tests sent `qgis_register_dialog_handler`
  themselves, named each handler, and a handler they forgot to remove
  stayed for the next test. `register_dialog_handler(predicate, button=...)`
  registers one (by default it presses `button` once) and returns its name,
  which is generated when not given; `unregister_dialog_handler(name)`,
  `list_dialog_handlers()` and `with qgis.dialog_handler(...)` go with it.
  The `qgis` fixture removes the handlers a test registered when the test's
  fixtures have been torn down, and a `once` handler that answered no
  dialog (the dialog the test expected did not come) raises
  `QgisUnusedDialogHandlerWarning`, as does one left at the end of a
  `with` block. `persist=True` keeps a handler across tests.

- **`object_name_re` in selectors** (#39). `object_name` matched only the
  whole name, while QGIS names some windows by a pattern (the attribute
  form opened for a feature is `featureactiondlg:<layer>:<feature id>`).
  `object_name_re` searches the name with a regular expression, as
  `text_re` does the text, in the live resolution and in snapshots.

- **`window` and `class_inherits` in selectors** (#38). No selector
  matched every top-level window, so a test could not wait for "no window
  is open" with `to_have_count(0)`; and `class` compares the Python type
  name, so a window that QGIS's C++ creates, such as the attribute form
  (`QgsAttributeDialog`), which sip wraps as a `QDialog`, could not be
  told apart. `window: true` matches windows (`isWindow()`), and
  `class_inherits` matches by the Qt class lineage (`QObject.inherits()`),
  which also sees Python subclasses. Snapshots do not carry either, so
  they work where a widget is looked up in QGIS (actions, `close()`,
  `to_be_visible` / `to_be_hidden` / `to_be_enabled` / `to_be_disabled`,
  `to_have_count`) and in
  dialog handler predicates; the snapshot-based lookups raise
  `LiveOnlySelectorError` instead of matching nothing.

- **All rows of a table, tree or list can be read** (#36). Snapshots stop
  at 100 rows so that a large table does not swell every response, which
  left no way to check the content of an attribute table. `read_rows()`
  (`Locator`, `E2EAutomationClient`, MCP `qgis_read_rows`) reads them in
  pages of up to 1000 from the Worker and joins them; `start` and
  `max_rows` read a part. `role` picks the value: `display` (default),
  `edit`, `user` (an ID kept on the row), `check` (`checked` /
  `unchecked` / `partial`), `tooltip`, or a Qt role number. `path` (as in
  #30) reads the children of a tree node, a legend's check states for
  instance. A disabled table can be read. `expect(locator).
  to_have_row_count(n)` waits for the number of rows, using the
  snapshot's `row_count`, which is not capped.

- **`title_contains` and `expect(locator).to_have_count(n)`** (#35). Waiting
  for a window to appear or close already works with a Locator and
  `expect(...).to_be_visible()` / `to_be_hidden()`, but `title` matched
  only the whole title, and nothing waited for "no such window" or "n of
  them", since `expect` refuses a selector that matches several.
  `title_contains` matches part of a title, in the live resolution and in
  snapshots. `to_have_count(n)` waits until n visible widgets match the
  selector, so `to_have_count(0)` waits for every matching window to
  close; it accepts several matches, ignores `index`, and does not count a
  window that was closed but not yet deleted.

- **`Locator.close()` closes a window as its close button does** (#34).
  Nothing let a test close a window the way a person does with the title
  bar's ×, so hosts looked up top-level widgets and called `close()`
  themselves. `close()` (`qgis_close_widget`, also an MCP tool) closes the
  window of the selected widget through its `closeEvent` (`reject()` for
  a `QDialog`) and returns `closed: False` when the window refused. It
  takes `wait=False` for a window that asks for confirmation on close. The
  QGIS main window is not closed.

- **A covered point is not clicked** (#33). Mouse input to the map canvas
  and clicks on a table, tree or list cell were sent straight to the
  viewport, so a point under a message bar item, a floating input over
  the canvas or another window was clicked although a person would hit
  the widget on top. The widget at the point (`QApplication.widgetAt()`,
  or the window's `childAt()`) is now checked first; anything other than
  the viewport or its children is `point_covered`, with `covered_by`.
  Widgets transparent for mouse events do not cover. A queued canvas
  action checks again when it is sent.

- **Leftover message bar items** (#32). A message bar item that stays until
  closed (a critical one, for instance) kept covering the top of the map
  canvas for every later test, in the way of clicks and screen
  comparisons. `message_bar` is a new `qgis_leftover_fail` /
  `qgis_leftover_warn` item: an item that was not there at the start and
  has no timeout is reported by level, title and text. Items that close
  themselves after a timeout are not counted, since they are only still
  showing. `qgis_get_leftover_state` returns the items with their
  `duration`.

- **A child row of a tree can be addressed** (#30). `cell` and the row value
  only reached the top level of a tree, so a layer inside a group, a
  setting under a section or a node of a browser tree could not be
  double-clicked, right-clicked or selected. `{"path": ["道路", "国道"]}`
  walks down from the top level, each step a row text (matched as `text`
  is) or a row number, and `column` applies to every step. Collapsed
  parents are expanded right before the click or selection, as a person
  would, and returned as `expanded`; nothing is expanded when the row is
  not found, and a tree that cannot be expanded is `row_collapsed`. A lazy
  model is asked to `fetchMore` first; a `row_not_found` there carries a
  hint to wait and retry. Failures carry the `depth` and `path` where they
  happened. `cell` in the result has the row `path`, and `value_after` of
  a child row is that path (`[1, 0]`; top-level rows stay a number).

  **The snapshot of an expanded tree grows.** A tree now also returns
  `child_rows` (the rows of each expanded node, top-down, as `{"path",
  "rows", "row_count"}`) and `collapsed_rows` (rows with children that are
  collapsed), and `current` of a child row has its `path`. `rows` keeps
  its shape. The names differ from a widget's `children` on purpose. The
  rows together are still capped at 100. Saved snapshots of expanded
  trees (a layer panel with open groups, for one) change, so review the
  difference before updating them.

- **A test can mark the leftovers it is known to leave** (#31). A product
  behaviour that always leaves, say, a map tool switched after certain
  tests produced the same warnings every run and buried new leftovers,
  while dropping the item from the ini hid it for every test.
  `@pytest.mark.qgis_leftover_expected("map_tool", reason="...")` keeps
  those items out of that test's report only; items can be listed, and
  marks on a class or module add up. A marked item that is not left
  behind issues `QgisLeftoverWarning` with the reason, so a fix is noticed
  and the mark removed (not for a test that failed in its call). Marks
  for items the ini does not look at do nothing; an unknown item or
  argument stops the run before any test.

- **State a test leaves behind is found and reported** (#28). The next
  test runs on the same QGIS, so a dialog left open, a layer left in
  editing mode or a map tool left switched changed its outcome without
  anyone seeing why. `qgis_leftover_fail` and `qgis_leftover_warn` (both
  empty by default) choose what to look at from `dialogs`, `popup`,
  `editing_modified` (unsaved changes or an undo history), `editing`
  (editing without changes), `map_tool`, `selection`, `layer_tree`
  (layer, group and legend item check states), `layers` (layers and groups
  added or removed), `active_layer` and `project_dirty`. The state is read
  right before the call and again after all fixtures have torn down, and
  only what was not there at the start is reported, as "what changed from
  what" (`建物 › 滅失済: checked True -> False`); so what a fixture makes and
  cleans up, or a window the host keeps open, is not reported, and the map
  tool is compared with the one at the start rather than with Pan. A
  `qgis_leftover_fail` item errors the test at teardown, and with
  `qgis_auto_fresh_after_failure = session` the session QGIS is restarted
  before the next test; a teardown error now counts as a failure for that
  in general. A `qgis_leftover_warn` item only issues
  `QgisLeftoverWarning`. Nothing is cleaned up, since that could destroy
  the user's data. The Worker reads the requested items in one call,
  `qgis_get_leftover_state`, which the MCP gateway also exposes. Checked
  on QGIS 3.34: a dialog left open errored the test and the restarted QGIS
  had none, and a layer left editing, a switched map tool and an unchecked
  category were warned about by name.

- **A hook right before pytest starts a QGIS** (#29). `spawn_qgis()` took
  fixed `args` and `env`, but nothing let a conftest prepare something for
  *each* start, such as a new profile in a temporary folder, so hosts
  patched internals. `pytest_qgis_before_spawn(config, kind)` is called
  before every start of the session QGIS (`kind` `"session"`, then
  `"restart"`) and of a `fresh_qgis` QGIS (`"fresh"`), and may return
  `{"args": [...], "env": {...}}` for that start only: the args go after
  `qgis_args` (or after a whole `qgis_command`), the env is added to the
  environment's; results from several conftests are combined, and two that
  set one variable to different values are a `UsageError`, as is a wrong
  return value. An exception in it starts no QGIS and errors the test. It
  is not called for a direct `spawn_qgis()` or in dev mode. The added args
  are logged, environment variables by name only. Making a fresh profile
  with only the needed plugins enabled is a user guide example rather than
  a built-in, since which plugins to install is the host's choice. Checked
  on QGIS 3.34 with that example: the session QGIS, its restart and a
  `fresh_qgis` QGIS each started on a new profile with only the core
  plugins and `qgis_puppeteer` active, and the folders were removed at the
  end.

- **Waiting for a popup** (#27). Modals had `wait_for_modal()` /
  `wait_for_modal_closed()`, popups had nothing, so a test that opened a
  menu with `click(wait=False)` polled `active_popup` itself.
  `E2EAutomationClient.wait_for_popup(title_contains=None)` waits for one
  (whose title contains the text, if given) and returns the `active_popup`
  node, `menu_items` included; `wait_for_popup_closed()` waits for it to
  go. The snapshot of a `QMenu` popup now carries its `title()`, the same
  title a dialog handler's predicate matches, since a menu's window title
  is usually empty. No helper that opens and chooses in one call is added:
  with the wait, it is `click(wait=False)`, `wait_for_popup()` and a
  `scope="popup"` click. The user guide also shows how to wait for windows
  with the existing tools.

- **Mouse and keys on the map canvas** (#26). QGIS's map tools (digitizing,
  moving, selecting) act on the mouse and key events the canvas receives,
  and there was no way to send them; tests built `QMouseEvent`s by hand
  through `qgis_execute_python`. `qgis_canvas_mouse` sends a click, double
  click, press, release, move, drag (with the button held through `steps`
  moves between points) or wheel to the canvas's viewport, and
  `qgis_canvas_key` sends a key (by Qt key name or a character; tap, or
  press and release apart for held keys) or types text character by
  character, to the canvas, the focused widget or a selector. Points are
  map coordinates in the canvas's CRS by default (another CRS with `crs`,
  viewport pixels with `pixel`), turned into pixels with the map transform
  at the moment of sending; a click moves to the point first so snapping
  indicators and rubber bands update as for a person; a point outside the
  viewport is refused before anything is sent (`point_outside_canvas`).
  The result gives the pixel, the `map_point` the map tool receives (up to
  half a pixel off the requested point), `map_units_per_pixel`, the current
  `map_tool` and the `extent` after sending. Both take `modifiers`, `wait`
  and a `canvas` selector for a canvas other than the main one. pytest gets
  `qgis.canvas()` with `click`, `right_click`, `dblclick`, `press`,
  `release`, `move`, `drag`, `wheel`, `press_key` and `type_text`, and the
  MCP gateway exposes both tools. Checked on QGIS 3.34: with the standard
  "Add Polygon Feature" tool, three clicks, a Backspace that removed a wrong
  vertex and a right click made one polygon whose vertices were within a
  pixel of the requested points, Escape cancelled a second one, and a wheel
  notch halved the extent's width.

- **A path can be set on QGIS's `QgsFileWidget`** (#25). The file / folder
  picker was `unsupported_widget_type`, so tests reached into its line edit
  by a class name read off the live widget, which a QGIS version could
  break. `qgis_set_widget_value` / `Locator.fill()` now take a single path
  string for it, set through `setFilePath()` and read back with
  `filePath()`, both public QGIS API; a path that does not exist yet is
  accepted (a save target or an output folder), and a spelling difference
  of the same place (`\` or `/`, a trailing separator) is not
  `value_not_applied`. A list of paths is refused (`invalid_value`), and so
  is a widget made read-only with `setReadOnly()`, which in QGIS 3.34
  disables the inner line edit while the widget itself stays enabled and
  has no `readOnly()` getter, so the inner line edit is looked at. The
  snapshot shows `file_path` and `storage_mode`. `qgis.gui` is imported
  only when a widget is looked at, so the module still loads in a Qt-only
  environment. Checked on QGIS 3.34: a `SaveFile`, `GetDirectory` and
  `GetFile` widget took paths with either separator, `filePath()` returned
  them unchanged, and the snapshot showed the path and the mode.

- **Table rows by cell text, double-click and right-click** (#24).
  `qgis_set_widget_value` on a table, tree or list view takes, besides a
  row number, `{"row": N}` or `{"text": cell text, "column": header name
  or index}` (any column when omitted; several matching rows are
  `row_ambiguous`), and selects the whole row through the selection model
  regardless of `selectionBehavior`, so `currentChanged` and
  `selectionChanged` fire as they do for a person. Row numbers follow the
  displayed order, hidden rows are skipped when matching text and refused
  by number (`row_hidden`), and trees are matched at the top level.
  `qgis_click_widget` takes `kind` (`left`, `double`, `right`) and `cell`
  (the same row forms): with either, it sends Qt mouse events to the
  widget or the view's viewport at the cell's centre, press / release /
  double-click / release for a double click so the first press selects the
  row before `doubleClicked` / `cellDoubleClicked`, and a
  `QContextMenuEvent` after a right click so `customContextMenuRequested`
  fires; a menu opened that way is answered with `wait=False` and
  `scope="popup"`. The default left click still uses `click()` /
  `trigger()`. The Locator gains `click(kind=, cell=)` and `dblclick()`,
  the client and the MCP tool pass `kind` and `cell` through. Checked on
  QGIS 3.34: a `QTableWidget` row chosen by its Japanese header and cell
  text fired `currentCellChanged` and `itemSelectionChanged`, a double
  click fired `cellDoubleClicked` after selecting, and a right click
  opened the host's context menu, whose item was chosen through
  `scope="popup"`.

- **Popup menus and file dialogs in E2E** (#22). A menu shown with
  `QMenu.exec()` is not modal, so neither `qgis_snapshot_ui` nor dialog
  handlers saw it. The snapshot has `active_popup` (`activePopupWidget()`;
  a `QMenu` also lists `menu_items` with `text`, `enabled`, `checkable`,
  `checked`, `separator` and `submenu`), and selectors take `scope="popup"`;
  a menu item is matched by `text`, also without `&` and a trailing `(&N)`
  (in the live tree and in snapshot lookups alike), and clicking it chooses
  it the way a person does, so `exec()` returns it (`QAction.trigger()` did
  not). Dialog handlers also answer popups, but only those whose predicate
  says `"popup": true`, so a catch-all `{}` does not close a combo box's
  drop-down; `action="button"` there chooses the item named by `button`.
  Native file dialogs on Windows are not Qt widgets and could not be
  answered: with `QPUPPETEER_QT_DIALOGS=1`, the plugin in a QGIS started by
  pytest (it has a launch token) sets `Qt.AA_DontUseNativeDialogs`, so
  `QFileDialog` and the like are Qt dialogs; a QGIS started by a person is
  left as it is. Checked on QGIS 3.34 (Japanese UI): an item of QGIS's own
  layer tree context menu was chosen by its text, a host menu item chosen
  by a handler led to a confirmation answered by another, and with the
  variable set a `QFileDialog` returned the file whose name was filled in;
  without it the attribute stayed off.

- **Operations that return before the confirmation they open, and dialog
  handlers that press a button** (#21). A button that opens a confirmation
  with `exec()` (`QMessageBox.question` and the like) kept `qgis_click_widget`
  from returning until the confirmation closed, so the call timed out: the
  Worker runs each request on the GUI thread, and the host's code stopped
  there. `qgis_click_widget`, `qgis_set_widget_value`, `qgis_select_features`
  and `qgis_set_canvas_extent` take `wait` (default True, as before);
  `wait=False` queues the operation with `QTimer.singleShot(0, ...)` and
  returns at once with `queued: true`. What can be checked on the spot (the
  widget is found, enabled, writable, of a supported type; the layer exists)
  is still returned, `prior_value` is read before queueing, and a failure
  inside the queued work is recorded in `qgis_get_recent_exceptions` as
  `QueuedOperationFailed` (or the exception's type), so
  `qgis_fail_on_uncaught_exception` fails the test. A target deleted before
  the queued work runs is not touched, since a C++-owned widget's Python
  wrapper does not notice and touching it crashes QGIS: the operation is
  recorded as failed instead. The pytest client, the
  Locator's `click` / `fill` / `select` / `check` / `uncheck` and the MCP tools
  pass `wait` through, and send it only when it is False. A dialog handler's
  `accept` did not count as pressing OK on a `QMessageBox` (`exec()` did not
  return `Ok`), so `action="button"` with `button` was added: a standard button
  name (`"Ok"`, `"Yes"`, `"No"`, ...) or the button's text, without `&` and a
  trailing `(&N)` (`"いいえ"` matches `いいえ(&N)`), for a `QMessageBox` or any
  dialog with a `QDialogButtonBox`. A missing button leaves the dialog open
  and is recorded the same way. Dialog handler actions are queued too, so a
  host's `reject()` that asks again no longer stops the polling; a modal that
  closes before its queued action runs is left alone. Checked on
  QGIS 3.34 (Japanese UI): a click, a combo box change, a selection and an
  extent change that each open a confirmation returned at once (the click in
  0.06 to 0.14 s) and their confirmations were answered by `button` handlers, by
  standard name and by Japanese text.

- **A hook to finish the host's own startup before startup messages are taken,
  and a marker that asks for a restart** (#19). A host plugin that shows a
  login dialog once the project opens is not ready when pytest takes the
  startup messages (#15), so what it logs while logging in was missed. The new
  hookspec `pytest_qgis_ready(config, client, instance_id, kind)` is called
  once per QGIS, right before its startup messages are taken, with `kind`
  `"session"`, `"restart"` or `"fresh"`; a conftest waits there, and its calls
  stay out of the test's `actions.jsonl`. An exception in it errors that test
  and the hook runs again for the next test on that QGIS. It is not called in
  dev mode. `@pytest.mark.qgis_restart_before` restarts the session QGIS
  before the marked test regardless of `qgis_auto_fresh_after_failure` (not
  for `fresh_qgis` tests), so host-specific restarts, such as before tests
  that write to a database, need no conftest logic of their own;
  `SessionQgis.start_count` tells a restart from the first start, and the
  restart is logged with its reason. Checked on QGIS 3.34: the hook ran for
  the session, the restarted and a fresh QGIS in turn, and a message logged
  inside it was among each one's startup messages.

- **pytest warns when the QGIS plugin and its own qgis-puppeteer differ** (#17).
  The plugin folder is a copy of the library with no version or commit, and
  the two are updated by different means (a lock file, a copy step, a uvx
  cache), so a fix could be present on one side only without anyone noticing.
  Both sides now compute a 12-hex fingerprint of the copied files
  (`qgis_puppeteer._fingerprint`; line endings and plugin-owned files do not
  count, so it matches whenever `sync_plugin.py --check` passes, and it does
  for a built wheel). The Worker sends it on registration, `list_instances`
  and `qgis_list_instances` return it as `source_fingerprint`, the MCP gateway
  names its own in its startup line, and pytest compares them once per
  session in `hub_ready`, raising `QgisPluginMismatchWarning` (also when an
  older plugin sends none). It does not fail the run unless asked with `-W
  error`. Checked on QGIS 3.34: no warning for a matching plugin, a warning
  after one line of the plugin copy was edited.

- **Messages logged while QGIS starts can be read and checked** (#15). The
  message check clears the Worker's record right before each test body, so
  what QGIS logged from startup until then (a project that failed to load, a
  plugin's init error, a startup step's own log) was never judged and could
  not be read. The first test that uses `qgis` on a QGIS now keeps those
  messages first — once per QGIS: the session one, a restarted one, each
  `fresh_qgis` one — and the new fixture `qgis_startup_messages` returns them
  (`SessionQgis.startup_messages` too). With `qgis_fail_on_message_level` set,
  they are checked against the same level and the ini allowlist; offending
  ones fail no test (they are no test's fault) but are listed in a
  "QGIS startup messages" section of the terminal summary, and the session
  exit status becomes failed. Markers do not apply to them. The check is
  public as `pytest_qgis_puppeteer.unexpected_messages(messages, config, *,
  item=None, min_level=None)`, for conftests that read messages themselves.
  Messages logged before the QGIS Puppeteer plugin loaded are still out of
  reach. Checked on QGIS 3.34 with a plugin that logs at startup.

- **`qgis_snapshot_ui` returns what lists, tables and trees show** (#12). It
  reported that a `QListWidget` or `QTableWidget` was there but not its
  contents, so tests read cells through `execute_python`. Item views now carry
  the model's display values: lists get `items` and `current_row`; tables and
  trees get `columns` (left out when the header is hidden, as in QGIS's layer
  panel), `rows` and `current`; all get `row_count`. Trees give their top level
  only. At most 100 rows and 50 columns are returned, beyond that
  `truncated: true` (and `column_count`). Values stay JSON: NULL becomes null,
  dates ISO strings, other types strings. Checked in QGIS 3.34: a 200-feature
  attribute table came back as 100 rows marked truncated, and the layer panel
  listed the layer.

- **`qgis_auto_fresh_after_failure = session` restarts the session QGIS after a
  failure** (#11). `true` runs only the next test on a fresh QGIS and then goes
  back to the session QGIS the failed test left dirty (an open dialog, loaded
  layers), so suites alternated between failing and passing. With `session`,
  the next test that uses `qgis` first closes the session QGIS and starts it
  again with the same settings; later tests run on the new one. A failed test
  that ran on `fresh_qgis` does not trigger it, a failed restart errors that
  test and is retried by the next one, and the restart's own calls stay out of
  the next test's `actions.jsonl` (new `E2EAutomationClient.unrecorded()`).
  The `qgis_process` fixture now yields a `SessionQgis` whose `restart()` a
  conftest can also call. Checked on QGIS 3.34: a restart took 6.5–7.1s, with a
  modeless or a modal dialog left open.

- **The MCP gateway can record what an agent does** (#8). For exploring a
  task through MCP and then writing it up as a pytest E2E test, the gateway
  appends every tool call to a JSONL file in the same shape as the pytest
  diagnostic bundle's `actions.jsonl` (`command`, `params`, `result`, `error`,
  `instance`, `duration_ms`, `step_path`, plus `source: "mcp"`). New tools
  `qgis_record_start(name)`, `qgis_record_step(name)`, `qgis_record_stop()`;
  `QPUPPETEER_MCP_RECORD=1` records the whole session. Files go under
  `QPUPPETEER_MCP_RECORD_DIR` (default `<TEMP>/qgis_puppeteer/recordings`);
  the agent names a recording but cannot choose the path. Results over 2000
  characters are truncated, images are noted by size, tool errors are kept
  whole. The record type moved to `qgis_puppeteer.action_log` so that both
  writers share it; `pytest_qgis_puppeteer._action_recorder` re-exports it and
  its output is unchanged. No code generation (ADR-0002 §16).

- **The Worker records QgsMessageLog, and tests can fail on it** (#5, roadmap
  B5c). Plugins often catch an exception and log it with
  `QgsMessageLog.logMessage(..., Qgis.Critical)`; that never reaches
  `sys.excepthook`, so the uncaught-exception check could not see it. The
  Worker now subscribes to `messageReceived` and keeps the last 1000 messages
  (`seq`, `ts`, `level`, `tag`, `message`, `source`, `thread`). Message bar
  text is included, because `QgsMessageBar` logs what it shows; it is marked
  `source: "message_bar"` (its tag is translated, so filter by source).
  Severity is ranked info = success < warning < critical — QGIS's numbers put
  Success above Critical. New Worker commands `qgis_get_recent_messages`
  (`min_level`, `tags`, `sources`, `after_seq`, `limit`) and
  `qgis_clear_recent_messages`; client methods `get_recent_messages()` /
  `clear_recent_messages()`. Opt-in ini `qgis_fail_on_message_level`
  (info / warning / critical) clears before the call phase and turns a pass
  into a failure, with `qgis_message_allowlist` (`regex` or `tag::regex`) and
  `@pytest.mark.qgis_allow_message(pattern, tag=, source=)` to ignore known
  noise. Failure bundles always carry `qgis_messages.jsonl`. Messages logged
  before the QGIS Puppeteer plugin loads (plugins earlier in name order) are
  not recorded.
- **MCP tools for failures that never reach the screen** (#5, #8):
  `qgis_get_recent_exceptions`, `qgis_clear_recent_exceptions`,
  `qgis_get_recent_messages`, `qgis_clear_recent_messages`. The Worker
  commands for exceptions existed, but an agent exploring through MCP could
  not ask whether its last action had failed behind the scenes.

- **`qgis_render_map`: draw the map at a fixed extent, CRS, size and DPI** (#6).
  `qgis_screenshot` saves whatever the canvas currently shows, so the image
  changes with the window size and the display scale and cannot serve as an
  image-snapshot baseline. The new Worker command renders with
  `QgsMapSettings` taken from the canvas (or the project when there is none),
  overrides extent / CRS / size / DPI / layers / background / drawing flags,
  pins the device pixel ratio to 1 and draws with the sequential renderer.
  Two renders with the same input give a byte-identical PNG (checked in real
  QGIS). The result carries the extent actually drawn (widened to the image
  aspect) and per-layer `render_errors`. Exposed as
  `E2EAutomationClient.render_map()` and the MCP tool `qgis_render_map`, which
  attaches the PNG to the result as an image (`include_image=false` to skip).
  The user guide lists what still makes images differ (unstyled layers get a
  random colour, network layers, time-dependent expressions, other machines).

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

- **The gateway says which build it is.** At startup it logs its package
  version, install source (git commit and URL, or the local path), code
  location, pid and Python version, to stderr and to `gateway.log`; a git
  install also reports `serverInfo.version` as `0.1.0+g<sha7>`, which Claude
  Code records at connection time. Asked for after the two-gateway mix-up,
  where the mere existence of a log file was taken as proof of a fresh build.

- **The Qt integration tests run again, and cover the field-reported scenarios.**
  `test_worker_integration` and friends were skipped everywhere — no PyQt5 in
  the venv or in CI — and had not been executed since ADR-0005 (two assertions
  still expected the old `worker-<label>-<pid>` ids). They now run in CI on
  Linux (PyQt5 is a dev dependency there) and on Windows through
  `scripts/qt_tests.py`, which drives QGIS's own Python so the Hub subprocess
  uses a consistent Qt. New `test_worker_liveness_integration` reproduces what
  was verified by hand: Hub killed → Worker respawns it and re-registers;
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

- **`title` is the title a person sees** (#37). A selector's `title` (and
  `title_contains`) matched the `windowTitle()` of any widget, so waiting
  for a window by its title also hit widgets inside it whose title nobody
  sees, such as forms left with Designer's default `"Form"`, and failed as
  ambiguous. `title` now matches a window's, a dock widget's or an MDI
  subwindow's `windowTitle()`, a menu's `title()` as before, and a group
  box's `title()`, which could not be matched before; other widgets have
  no title. Snapshots report `title` by the same rule, so saved snapshots
  lose the titles of inner widgets and gain those of group boxes.

- **A click or value change that fails raises, and a value Qt changed on the
  way in is a failure** (#23). `E2EAutomationClient.click_widget()` /
  `set_widget_value()` and the Locator's `click` / `fill` / `select` /
  `check` / `uncheck` returned a `success: false` dict, so a test went on
  without noticing nothing was clicked or set; they now raise
  `WidgetOperationError` (`error`, `widget`, `result`), and the MCP tools
  return a tool error. There is deliberately no switch to get the dict back;
  a test that expects a failure catches the exception. `qgis_set_widget_value`
  also refuses what Qt would silently drop: an index out of range
  (`index_out_of_range`, for combo boxes, tabs, lists and views), a value of
  the wrong form (`invalid_value`: a date or time not in the editor's form, a
  check box value other than true / false or `"true"` / `"false"`, since
  `bool("false")` is true, or a non-integral number for a `QSpinBox`). After
  setting, it reads the value back, returns it as `value_after`, and fails
  with `value_not_applied` when it differs (a clamped number or date, a text
  cut by `maxLength`, an exclusive radio button that stays checked).
  Subclasses, QGIS's and the host's, are compared like the Qt classes they
  derive from: the read-back uses the base class's API, so no list of
  widgets has to follow QGIS versions; a field that reshapes its value takes
  `verify=False` on the caller's side. With `wait=False`, only the checks made before setting come
  back; the rest is recorded in recent_exceptions. A line edit with a
  validator also reports `acceptable_input`, because `setText` bypasses the
  validator. Also fixed: choosing a `QListWidget` item by text always failed
  (`findItems` got the integer 0 instead of a match flag), and `QDateEdit` /
  `QTimeEdit` were read in the date-time form and ignored their own;
  `prior_value` for them is now `"yyyy-MM-dd"` / `"HH:mm:ss"`. Checked on
  QGIS 3.34: a list item chosen by text, a clamped `QSpinBox` and a truncated
  `QLineEdit` raised with `value_after`, `"false"` unchecked a check box, a
  `QDateEdit` took `"2026-10-20"`, and a clamped `QgsSpinBox` raised too.

- **The QGIS plugin is now a self-contained `qgis_puppeteer` folder, named
  "QGIS Puppeteer".** The plugin folder used to be `plugins/qgis_puppet`
  (display name "QGIS Puppet") and required `qgis-puppeteer` to be
  pip-installed into QGIS's Python separately. Now it is
  `plugins/qgis_puppeteer/` and is the `qgis_puppeteer` package: copy (or link)
  it into the plugin directory and nothing else is needed — not even
  websockets, which only the client and the MCP gateway use and which are now
  imported lazily. The GPL plugin code is `plugins/qgis_puppeteer/qgis_plugin/`;
  the rest of the folder is a committed copy of the library, generated by
  `scripts/sync_plugin.py` and checked in CI. The Apache-2.0 wheel and sdist
  contain no plugin code (CI checks both).
  To migrate: remove the old `qgis_puppet` plugin folder, **remove any
  `qgis_puppeteer` from QGIS's Python** (user site, a host application's vendor
  directory), install the new folder and enable "QGIS Puppeteer" — plugin
  enablement is stored per plugin id, so it has to be switched on again. If
  another `qgis_puppeteer` was imported before QGIS loaded the plugin (by a
  `PYQGIS_STARTUP` script or an earlier plugin), Python hands that copy to
  QGIS; the plugin then refuses to load and names that copy. Extension plugins are
  unaffected: `puppeteer_api.build_handlers` is discovered as before. The Hub
  spawn lock and log are now `qgis_puppeteer-hub-<port>.lock` and
  `qgis_puppeteer.spawn.log` under `<TEMP>`, and the spawned Hub puts its own
  package first on `sys.path`, so it runs the same copy as the plugin even
  when the host passes a `sys.path` containing another one.

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

- **`spawn_qgis` waited out the register timeout when the launch command had
  already failed.** It watched only the Hub, so a launcher that printed an
  error and exited with code 1 (a wrong path or a missing setting) left the
  test waiting the full `register_timeout_s`, 60 s by default, and then
  failing with "did not register" and no hint of why; the launcher's stderr
  was captured but not shown. `spawn_qgis` (and so the session QGIS and its
  restart) now checks the launch process while it waits. If it exits with a
  non-zero code before QGIS registers, it fails at once with
  `WorkerLaunchFailed`, a subclass of `WorkerRegisterTimeout`, whose message
  carries the exit code, the command and the last 10 lines of stderr (also in
  `returncode` and `stderr_tail`), after the same cleanup as a timeout. An
  exit code of 0 still waits, since some launchers start QGIS and exit 0 at
  once. A plain timeout now also shows the last lines of stderr. To support
  this, `E2EAutomationClient.wait_for_ready()` takes `on_poll`, called before
  each wait; raising from it ends the wait with that exception.

- **`expect(...).to_be_disabled()` passed when no widget matched.** A widget
  that is not found comes back with `enabled: False`, and the assertion looked
  only at that, so a selector with a typo passed at once; so did
  `not_.to_be_enabled()` and the MCP tool's `qgis_wait_for(until="disabled")`.
  All three now also need the widget to exist, as Playwright's
  `toBeDisabled` does: they wait for it to appear disabled, and on timeout fail
  with the last check (`exists: False`); the pytest message says that no
  widget matched. `to_be_hidden()`, `not_.to_be_visible()` and
  `until="hidden"` still pass when nothing matches.

- **Negated value assertions passed when no widget matched.** A widget missing
  from the UI snapshot reads as the value None, which differs from any expected
  value and is not "checked", so `not_.to_have_text()`, `not_.to_contain_text()`,
  `not_.to_have_value()`, `not_.to_be_checked()` and `not_.to_have_row_count()`
  passed at once on a selector with a typo; so did the MCP tool's
  `qgis_wait_for(until="unchecked")`. The value assertions, negated or not, and
  the MCP tool's text, value, check and row-count conditions now need the
  widget to be in the snapshot, as in Playwright: they wait for it to appear,
  and on timeout fail with `found: False`, which `qgis_wait_for` also reports
  in `observed`. A widget that is found but has no such value still reads as
  None.

- **`--env=all --junit-xml` wrote a report with no tests.** Each env's child
  pytest wrote to the same file, overwriting the one before, and then the
  parent, which runs no tests itself, wrote its own empty report over all of
  them, so a CI reading the file saw zero tests and no failures. The
  `<env>::` prefix on `classname` that the guide described was never applied
  either: it ran before pytest wrote the file. With `--env=all` or
  `--env=A,B`, each child now writes to `outputs/<env>/junit.xml`, and the
  parent merges them into the `--junit-xml` file: one `<testsuite>` per env,
  named after it, each `classname` prefixed with `<env>::`, and the totals on
  `<testsuites>`. An env that did not run (cumulative `--maxfail`, or a hard
  error in an earlier env) appears as a testsuite with one skipped test, and
  one that ran but left no readable report as a testsuite with one error, so
  no env drops out silently. A single `--env` still writes pytest's own
  report, without the prefix.

- **Code run in trusted mode was not recorded anywhere.** With
  `QPUPPETEER_TRUSTED_MODE=1`, `execute_python` runs code without asking, and
  the record of what ran went to a Python logger that nothing inside QGIS
  listens to. It is now written to `QgsMessageLog` under the tag
  `QGIS Puppeteer` as `[trusted mode] exec: <first 200 characters>`, so it
  shows in QGIS's log panel and, in pytest, in the diagnostic bundle's
  `qgis_messages.jsonl`. The level is NoLevel (`"none"`), below `min_level`
  checks: pytest's QGIS always runs in trusted mode, and an Info record would
  fail every test that calls `execute_python` under
  `qgis_fail_on_message_level = info`.

- **A hung QGIS started through a launcher was left running** (#43). When
  QGIS stops answering heartbeats for long enough, the Hub drops it from
  the instance list. `spawn_qgis` (and so the session QGIS, its restart and
  the session teardown) looked up the QGIS pid in that list when cleaning
  up, so it stopped only the launcher, and the QGIS window stayed open,
  slowing anything that walks the desktop's windows. `spawn_qgis` now
  remembers the QGIS pid when it registers. If the QGIS is no longer
  listed at teardown, it checks that the pid is still the QGIS it started
  (creation time and launch token on Windows, process group on posix),
  asks it to close, kills it if it is still there after the graceful
  timeout, and logs a warning.

- **The QGIS Python launcher was found only for an LTR QGIS, and in pytest
  hardly ever.** Discovery looked only for `python-qgis-ltr.bat`, the name in
  an LTR install; a regular install has `python-qgis.bat` next to
  `qgis-bin.exe`. It also looked only under `OSGEO4W_ROOT/bin` and next to
  `sys.executable`. pytest runs on a venv's Python and a plain shell does not
  set `OSGEO4W_ROOT`, so without `qgis_python` the Hub could not start even
  with `qgis_bin` set. `find_qgis_python_launcher()` now takes `qgis_bin` and
  looks next to it first, and in each folder picks the launcher matching the
  QGIS executable's name (`qgis-ltr-bin.exe` → `python-qgis-ltr.bat`,
  `qgis-bin.exe` → `python-qgis.bat`), then either name. pytest passes
  `qgis_bin`, so setting `qgis_bin` is enough. In a regular QGIS, the plugin
  now starts the Hub through `python-qgis.bat` instead of the bare
  `pythonw.exe` next to `qgis-bin.exe`.

- **`qgis_screenshot` failed on a canvas that had drawn nothing** (#42).
  A canvas to which no layer was ever added has an empty extent and never
  draws, so `saveAsImage` left only a `.pgw` and the tool reported that the
  image was not written. Such a canvas is now saved as its background
  colour, at the canvas's size. While QGIS shows its welcome page instead
  of the canvas (no project or layer opened yet), the tool says so instead
  of saving the hidden canvas.

- **`text_contains`, `text_re`, `role` and `attr` were ignored when acting
  on a widget** (#35). They were applied when a selector was resolved in a
  snapshot (`expect`, `wait_for_widget`, `snapshot()`), but not when one
  was resolved for an operation (`click`, `fill`, actionability), so
  `{"class": "QPushButton", "text_contains": "保存"}` matched every button
  there and failed as ambiguous, or clicked the only button of that class
  whatever its text. The live resolution now judges these keys on the
  same record as the snapshot.

- **A failed entry into a password field left its value in the MCP action
  log** (#23). Since a failed `qgis_set_widget_value` became a tool error,
  `masked` sat under `error.details`, which the recorder did not look at,
  so the value was written in clear text. It now redacts it there too.

- **Leftover checks and the start hook, after review** (#28, #29). The
  state is now read right after the `qgis` fixture is ready rather than
  right before the call, so what a test's own fixture makes and cleans up
  (a layer added then removed, a selection restored) is no longer reported
  as removed. A state that cannot be read is reported once with
  `QgisLeftoverWarning` instead of passing silently. A test that already
  failed in its call gets its leftovers as a warning, not a second error.
  Only teardown failures of tests that used QGIS ask for a restart.
  `selection` notices a different set of the same size. An environment
  variable set by the environment or a `fresh_qgis` marker now wins over
  the same one from `pytest_qgis_before_spawn`.

- **Table rows, popups and dialog handlers, after review** (#22, #24, #27).
  A list view with `setModelColumn` was clicked and selected in the hidden
  column 0; a row picked without a column now uses the first column on
  screen. A column shrunk to zero width counts as hidden, like one hidden
  with `setColumnHidden`. A click is checked to land on the cell first
  (`click_missed`, `cell_disabled`, `row_gone` for a row removed before a
  queued click, which also keeps its place with a persistent index). Cell text is
  matched as shown (an empty cell is `""`, booleans `true` / `false`,
  numbers and dates as the delegate displays them), columns past the
  snapshot limit can be addressed, duplicate header names are
  `column_ambiguous`, `{"row", "text"}` together is refused, and duplicate
  `QListWidget` texts are `row_ambiguous`. A popup's `title` in the live
  selector is the menu's `title()`, as in the snapshot and dialog
  handlers; hidden menu items are not candidates; `&&` is a literal `&`;
  `wait_for_popup` ignores `&` in titles. A `once` handler that could not
  be applied is kept for the dialog it was meant for, and with no handlers
  the tick no longer writes a property onto host dialogs. A read-only
  `QgsFileWidget` is reported as not editable, so `fill()` waits for it.

- **Canvas input, after review** (#26). A queued mouse action checks the
  point against the map at the moment of sending and records
  `point_outside_canvas` instead of sending it; the CRS transform is built
  then too, and a transform error is `transform_failed`. The in-viewport
  check uses the rounded pixel the map tool receives. `crs` with `pixel`
  and drags of more than 1000 steps are refused. Typed text sends Return
  and Tab for newlines and tabs and Shift for capitals. `Canvas` takes
  `pixel=True`.

- **A double or right click on a table row picked by a hidden column's text
  went to the hidden cell** (#24). A table that keeps an ID column hidden
  to identify rows, matched with `cell={"text": id, "column": "ID"}`, got
  the mouse events at the hidden cell's empty rectangle, so the row was
  never hit. The click now goes to the first visible column of that row,
  in on-screen order; with no visible column it is `no_visible_column`.

- **A dialog handler fired again and again on a modal it did not close, and
  could skip a new one** (#21). The handler tracked the modal it had handled
  by `id()` of the Python wrapper, but PyQt may return a new wrapper for each
  `activeModalWidget()` call, so the same modal looked new on every tick, and
  a closed modal's address reused by the next one made that one look handled.
  The modal now carries its own key in a dynamic property. Registering a
  handler re-evaluates the modal that is open, which the changing `id()` used
  to do by accident.

- **A selector's `index` picked the first candidate when candidates looked
  alike** (#20). `_finalize_match` mapped the chosen summary back to its widget
  with `list.index`, which finds the first equal summary, so two widgets with
  the same class and object name (two unnamed `QTabWidget`s, one in a dock)
  always resolved to the first whatever `index` said. It now maps by identity.
  This was the only such lookup, but every widget RPC goes through it — click,
  set_value, actionability and nested Locators. `root_object_name` also only
  looked at top-level windows; when none has the name, a visible widget with
  that object name inside a window (a dock, a panel) becomes the root, and
  several such widgets are `selector_ambiguous`. Hidden widgets are left out,
  so a closed dock or a closed window not yet deleted does not count.

- **A QGIS that never sent a registration was left running** (#18). When the
  plugin does not load, the QGIS never reaches the Hub, so neither the
  instance list nor the refusal record gives its pid, and a launcher that has
  already exited leaves nothing to stop. When the startup wait gives up with
  nothing of its own on the Hub, `spawn_qgis()` now finds the QGIS by its
  `launch_token`, which travels in the environment through every launcher
  step (cmd, Python, `start`). On Windows it looks at processes whose
  executable name starts with `qgis` (`qgis-ltr-bin.exe`) and, through one
  handle each, checks that the process was created between the launch and the
  timeout, reads its `QPUPPETEER_LAUNCH_TOKEN` from the process environment
  (Toolhelp32, NtQueryInformationProcess and ReadProcessMemory through ctypes;
  64-bit Windows 8 or later), and terminates it through that same handle when
  the token is its own, so a reused pid cannot redirect the kill. Parent links
  are not followed: once a launcher step exits, the live process list no longer
  connects QGIS to the launcher. A QGIS started by hand carries no token.
  Processes that cannot be inspected (no access, already gone) are skipped and
  counted in a warning. On posix the QGIS command runs in its own process
  group, which is killed. A launcher that drops the environment, or a renamed
  QGIS executable, is still out of reach. Checked on QGIS 3.34 with a profile
  that lacks the plugin, started through a launcher whose intermediate `cmd`
  steps exit at once: the QGIS was found by its token and killed when the wait
  gave up, and none was left.

- **A QGIS that registered only after `spawn_qgis()` stopped waiting was left
  running** (#16). When the wait gives up, `spawn_qgis()` now tells the Hub
  that it abandoned the `launch_token` (new client-to-Hub message
  `abandon_launch`; no reply, so an older Hub that cannot read it only answers
  with an error the client drops). The Hub refuses that token's registrations
  for the next 30 minutes with `label_conflict` and
  `details.reason = "launch_abandoned"` (an existing code, so older plugins
  can read it), and records them like other refusals. The QGIS Puppeteer
  plugin closes its QGIS when it was started with a `launch_token`, has never
  registered, and is refused for any reason; it uses `close_qgis`, falls back
  to exiting after 12 seconds, and logs why to QgsMessageLog and stderr. A
  QGIS that has registered once does not close on a refused reconnect, and a
  QGIS started by hand has no token. `register_failed` now carries the
  refusal's `details.reason` (`label_conflict: too_long`). Still out of reach:
  a QGIS that tries to register after the Hub has stopped, and one that never
  sends a registration (#18). Checked on QGIS 3.34: with a 2-second wait, the
  late QGIS was refused and closed itself, and none was left.

- **Closing a QGIS that has a modal dialog open** (#16 design, used by #9 and
  #11). The teardown and the session restart asked QGIS to close its main
  window, which does nothing while a modal dialog runs its own `exec()` loop —
  a host's login dialog shown while the project opens is enough — so they
  waited 15 seconds and then killed the pid. QGIS now closes through
  `qgis_puppeteer.qgis_tools.app_tools.close_qgis`: in rounds a fifth of a
  second apart it discards changes (project dirty flag, edits in editable
  layers, so no save prompt appears), rejects the topmost modal through a
  queued `reject()` (a dialog whose `reject()` asks a question of its own does
  not block the procedure, and the question is rejected in the next round),
  and closes the main window once no modal is left. It gives up after about 10
  seconds and leaves the kill to the caller, as before. A QGIS whose plugin
  predates this is asked the old way. Checked on QGIS 3.34: with a modal open
  a session restart took 8.9s instead of waiting out the 15s; with a dialog
  that refuses to close, the kill still ended it.

- **A QGIS that did not become usable within the startup timeout was left
  running** (#16, first part). On `WorkerRegisterTimeout` `spawn_qgis()` only
  stopped the process it had started, which a launcher such as
  `qgis-ltr.bat` has already left. It now looks once more for its own
  `launch_token`: in the instance list (registered but still blocked, for
  example by a slow project load) or among the Hub's refused registrations
  (#13). If the QGIS is there its pid is known, and it is killed — not asked
  to close, since a blocked GUI would only answer at the request timeout. A
  QGIS that registers only after the wait gave up, or never registers, is
  still out of reach.

- **On Windows the Hub that pytest started stayed behind after the session**
  (#14). The Hub runs under `qgis_python`, by default `python-qgis-ltr.bat`, so
  the process pytest held was `cmd.exe` and the Hub's python its child;
  `terminate()` stopped `cmd.exe` only ("Hub subprocess terminated
  (returncode=1)" was cmd's exit), and the Hub kept its port for minutes until
  its idle shutdown. The teardown, and the clean-up after a Hub that never
  started listening, now stop the whole tree (`taskkill /T /F`; on posix the
  Hub runs in its own process group, which is signalled), then check that the
  port closed. If the launcher had already gone, the process still listening
  on the port is found through `netstat` and stopped; otherwise a warning says
  the port is still open. Checked on QGIS 3.34: no Hub was left after a
  session.

- **`fresh_qgis` QGIS were not registered when the test name was long, and stayed
  behind** (#13). The Hub took worker labels of at most 32 characters, and
  `fresh_qgis` labels its QGIS `fresh-<test name>`, so a test name over 26
  characters was refused; nothing told the waiting side, which gave up after
  `qgis_startup_timeout` with "did not register", and with a launcher that
  exits early the QGIS was left running. Now:
  - The label limit is 128 characters (ADR-0001 §5 had no reason for 32, and
    nothing depended on it). A `fresh_qgis` label that would still be longer
    uses the parameter numbers instead of their values
    (`fresh-test_open[2]`); the full test id is logged with the instance and
    written to the diagnostic bundle's `fresh_qgis.json`.
  - `spawn_qgis()` raises `ValueError` before launching when the label it is
    given (`label=` or `QPUPPETEER_WORKER_LABEL`) is one the Hub would refuse.
    It does not rewrite labels the caller chose.
  - The Hub remembers refused registrations per `launch_token` (the latest 64)
    and returns them from `list_instances` when asked with
    `include_rejected=True` (`RejectedRegistration`, with the reason and the
    QGIS pid; older Hubs return none). `wait_for_ready(launch_token)` raises
    `RegistrationRejectedError` at once instead of waiting, and `spawn_qgis()`
    then kills that QGIS by pid and raises `WorkerRegisterRejected`, a
    subclass of `WorkerRegisterTimeout`. The MCP tool `qgis_wait_ready`
    returns the tool error `registration_rejected` with the record.
  Checked on QGIS 3.34: a fresh test with a 180-character parameter registered
  as `fresh-test_…[0]`, and a QGIS refused for a live label failed in 7s
  (QGIS startup included) with the reason, and was not left running.

- **Password fields leaked their text** (#10). `QLineEdit`s in `Password`,
  `PasswordEchoOnEdit` or `NoEcho` mode, and QGIS's `QgsPasswordLineEdit`, were
  read like any other field, so a password typed by a test or a person ended up
  in MCP responses (the agent's context) and in the diagnostic bundle. Such
  fields now report `"masked": true` and no `text` in `qgis_snapshot_ui`, in the
  widget summaries of click / set_value / actionability results, and through
  `Locator.get_text()` / `get_value()`; a `text` selector never matches their
  contents; `qgis_set_widget_value` returns `prior_value: null` and
  `masked: true`; and the `value` put into one is written as `"***"` in
  pytest's `actions.jsonl` and in MCP recordings
  (`qgis_puppeteer.action_log.redact_secret_value`). Read the contents with
  `execute_python` when a test really needs them.

- **`fresh_qgis` crashed the QGIS it was cleaning up when `qgis_command` is a
  launcher that exits early** (#9). The teardown only stopped the process it
  had started; with a launcher already gone it fell through to the
  dangling-worker path, which ran `QgsApplication.exitQgis()` inside the live
  QGIS. That function is the last step of shutdown, not a way to quit, and the
  QGIS crashed and stayed behind after a 30-second teardown. The teardown now
  stops the Worker's own pid from `list_instances`: it asks QGIS to close its
  main window (discarding project changes, so no save prompt), waits for the
  pid to go, and otherwise force-kills that pid alone, without `/T`. The
  session teardown closes leftover Workers the same way instead of sending
  `taskkill /T` to each pid.

- **`qgis_screenshot` saved a transparent image headless, and reported sizes it
  did not use** (#7). Without a canvas it started a render job but saved the
  empty image it had prepared instead of the job's result; it now renders with
  `qgis_render_map`'s renderer, over the layers shown in the layer tree (it
  used to take every project layer, hidden ones too). With a canvas,
  `QgsMapCanvas.saveAsImage` ignores `width` / `height`, yet the result echoed
  them back; it now returns the size of the saved file, and says so when
  nothing was written. The
  diagnostic bundle's `screenshot.png` was described as the main window; it is
  the map canvas only, and the docs now say so.

- **`spawn_qgis()` could return an instance that cannot answer.** Its
  launch_token wait matched on the token alone, and `list_instances` now also
  returns instances whose heartbeat has stopped, so a QGIS still busy loading
  was reported as spawned and the caller's first command blocked until the GUI
  thread freed up. `wait_for_worker()` gained the `state == "active"` filter
  when the state was introduced; this path did not. It now shares
  `wait_for_ready()` with the rest.

- **The "bare `Error executing tool` after a Hub swap" was a second, stale
  gateway.** Five rounds of field verification pointed at the gateway's error
  handling; every faithful reproduction recovered cleanly. The process list
  finally showed why: Claude Desktop's `claude_desktop_config.json` also
  defined `qgis-puppeteer`, launched with `uvx --from <vendor dir> --with mcp`,
  and the Desktop app injects that shared-pool server into Code-tab sessions
  next to the project's `.mcp.json` entry of the same name. `uvx --from <local
  dir>` never rebuilds when only `src/` changes (uv 0.8: only `--refresh`,
  `--reinstall-package` or `uv run` notice), so that copy was months old — no
  `_with_client`, no `qgis_wait_ready`, no log file — which is exactly what the
  session saw: a tool missing from the list, a silent `gateway.log`, discovery
  tools crashing while worker commands (which had a stale-connection retry for
  longer) worked. Nothing in the current gateway was wrong. The guide gains a
  "is a second gateway running?" check and the uvx path-source caveat, and
  `pyproject.toml` declares `[tool.uv] cache-keys` so `uv run`/`uv sync` from a
  local copy follow source changes.

- **Every MCP tool is guarded, and the gateway keeps a log file.** Field
  verification of f49f8f0 still produced a bare `Error executing tool
  qgis_list_instances` right after a Hub swap — instantly, twice, then fine
  once a worker command had run — and neither a faithful local reproduction
  (Qt Worker respawning a real Hub, stdio gateway on Python 3.13 / websockets
  17) nor Claude Code's own MCP log (events only, no stderr) showed the
  exception. So the fix stops depending on knowing it: `_register_tools` wraps
  every tool in `_guard_tool`, so an exception raised anywhere in a tool body,
  including before `_with_client`, comes back as `gateway_internal_error` with
  its type and traceback, and `main()` mirrors stderr into
  `<TEMP>/qgis_puppeteer/gateway.log` (`QPUPPETEER_GATEWAY_LOG`), where the
  SDK's own "unexpected exception" traceback also lands.

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
