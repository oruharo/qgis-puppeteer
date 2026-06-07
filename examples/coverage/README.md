# E2E coverage — measuring code that runs *inside* QGIS

E2E tests run in **two processes**: the pytest **runner** (your test code +
the client library) and the **QGIS** process (the actual code under test,
driven over WebSocket). Plain `pytest --cov=...` only measures the *runner*
process, so it misses the QGIS-side plugin/app code — usually the part you
care about.

To measure the QGIS-side code, use `coverage.py`'s **subprocess** mechanism.
qgis-puppeteer makes this easy because it **propagates the pytest process
environment to the spawned QGIS** (the spawn env is built from `os.environ`),
so a couple of env vars are enough to switch coverage on inside QGIS.

## Files here

| File | Purpose |
|---|---|
| `.coveragerc` | Coverage config (parallel + thread concurrency + your `source`). Copy to your project root. |
| `startup.py` | **Recommended.** QGIS *profile* startup hook that calls `coverage.process_startup()`. Profile-scoped, no `site-packages` changes. |
| `coverage_subprocess.pth` | Alternative to `startup.py`: drop into the QGIS Python's `site-packages` to activate at interpreter init (earliest, but interpreter-wide). |

## Setup (one time)

1. **Install coverage into the QGIS-bundled Python** (it is a *different*
   interpreter from your pytest venv):

   ```bash
   <qgis-python> -m pip install coverage
   ```

   (`<qgis-python>` is e.g. `python-qgis-ltr.bat` on OSGeo4W, or the
   `qgis_python` you configured for qgis-puppeteer.)

2. **Edit `.coveragerc`** — replace `your_package` with the package(s) you want
   measured — and copy it to your project root.

3. **Enable the startup hook** (pick one):

   - *Profile hook (recommended):* copy `startup.py` to your test profile's
     `python/startup.py` (see the header in that file for the per-OS path). If
     you launch QGIS with a dedicated `--profile=test`, this only affects that
     profile.
   - *Interpreter-wide:* copy `coverage_subprocess.pth` into the QGIS Python's
     `site-packages/`. The filename only needs to end in `.pth`. This activates
     even earlier (best import-time fidelity) but applies to every run of that
     interpreter. Both are no-ops when `COVERAGE_PROCESS_START` is unset.

## Run

```bash
# Absolute paths so the runner AND the (different-cwd) QGIS process agree.
export COVERAGE_PROCESS_START="$PWD/.coveragerc"
export COVERAGE_FILE="$PWD/.coverage"

# Optionally also measure the runner side:
pytest --cov=your_runner_side_pkg test_e2e/    # or just: pytest test_e2e/

coverage combine     # merge runner + every QGIS process data file
coverage report -m   # or: coverage html / coverage xml
```

On Windows `cmd`: use `set VAR=value`; on PowerShell: `$env:VAR="value"`.

## Caveats

- **Graceful shutdown is required.** coverage flushes its data in an `atexit`
  handler, so the QGIS process must exit cleanly. The `qgis_process` /
  `spawn_qgis` / `fresh_qgis` paths shut QGIS down gracefully; a hard kill loses
  that process's data.
- **Import-time fidelity.** A profile/`.pth` hook runs *earlier* than QGIS
  plugins load, so it captures module-load (top-level) lines of your code. A
  hook that started only when a plugin loads would miss those — that's why this
  is not built into the `qgis_puppet` plugin.
- **`source` / `[paths]`.** Point `source` at your package, not at
  qgis-puppeteer. If the path seen inside QGIS differs from the runner's view,
  add a `[paths]` mapping so `coverage combine` merges them.
- **External / dev-mode QGIS.** If you reuse an already-running QGIS
  (`QPUPPETEER_E2E_USE_RUNNING_QGIS=1`) or launch via a custom `qgis_command`
  wrapper, env inheritance is your responsibility: start that QGIS with
  `COVERAGE_PROCESS_START` (and `COVERAGE_FILE`) set, or have the wrapper
  forward them.

See the user guide section **"E2E カバレッジ計測"** for the rationale and the
runner-side / multi-env coverage notes (ADR-0003).
