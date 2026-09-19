# Contributing to qgis-puppeteer

Thanks for your interest in improving qgis-puppeteer. This guide covers
the bare essentials. The project is small enough that most decisions are
made via issue discussion before code is written.

## Where to discuss

- **Bug reports / feature requests:** open a [GitHub Issue](https://github.com/oruharo/qgis-puppeteer/issues).
- **Larger changes (new tools, protocol additions, API changes):** open
  an issue first to align on the design before sending a PR. Architecture
  decisions live in [`docs/architecture/`](docs/architecture/) as ADRs.

## Repository layout

| Path | Distributed via | License |
|---|---|---|
| `packages/qgis-puppeteer/` | PyPI (`qgis-puppeteer`) | Apache-2.0 |
| `packages/pytest-qgis-puppeteer/` | PyPI (`pytest-qgis-puppeteer`) | Apache-2.0 |
| `plugins/qgis_puppeteer/` | QGIS Plugin Repository. The ready-to-copy plugin folder: `qgis_plugin/`, `metadata.txt`, `icon.png`, `LICENSE` are its own; everything else is a generated copy of the library | GPL-3.0-or-later (own files); the copied library files stay Apache-2.0 |
| `plugins/tests/` | — (tests for the plugin) | GPL-3.0-or-later |
| `docs/architecture/` | — | Project docs (ADRs) |
| `examples/` | — | Sample scripts and tests |

The two PyPI packages and the QGIS plugin share the same git repository.

QGIS imports a plugin by its folder name, so the plugin folder has to *be*
the `qgis_puppeteer` package. `plugins/qgis_puppeteer/` is therefore committed
as a complete, ready-to-copy folder: the plugin's own files (`qgis_plugin/`,
`metadata.txt`, `icon.png`, `LICENSE`) plus a copy of the library from
`packages/qgis-puppeteer/src/qgis_puppeteer/`. The library under `packages/`
is the source of truth — **never edit the copy by hand**. After changing the
library, refresh the copy and commit both together:

```bash
uv run python scripts/sync_plugin.py          # refresh plugins/qgis_puppeteer/
uv run python scripts/sync_plugin.py --check  # what CI runs
```

The library must not contain `qgis_plugin/` or `metadata.txt` (the sync script
refuses, and CI checks the wheel): their absence is how `classFactory` notices
that QGIS was handed a pip-installed copy instead of the plugin.

To try the plugin from a checkout, link the folder into a QGIS profile
instead of copying it (Windows, from the repo root):

```bat
mklink /J "%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\qgis_puppeteer" "plugins\qgis_puppeteer"
```

## Development setup

The project uses [`uv`](https://docs.astral.sh/uv/) for workspace and
dependency management.

```bash
git clone https://github.com/oruharo/qgis-puppeteer.git
cd qgis-puppeteer
uv sync --all-extras --dev
```

Run tests per package (the suites are kept separate so failure
attribution is clear; running them at once from the workspace root
hits a `tests/__init__.py` module-name collision):

```bash
uv run pytest packages/qgis-puppeteer/tests
uv run pytest packages/pytest-qgis-puppeteer/tests
uv run pytest plugins/tests
```

All tests are mocked — you do **not** need a running QGIS to run the test
suite. Real-QGIS integration tests are out-of-tree and run manually.

## Code style

- Python ≥ 3.10. Type hints required on public APIs.
- Formatter: `ruff format` (line length 100, configured in root
  `pyproject.toml`).
- Linter: `ruff check`. CI currently runs lint as a soft gate
  (`continue-on-error`) while we work down the existing baseline; once
  clean, it will become a hard gate.
- Tests use `pytest`. Mock `subprocess.Popen` / `subprocess.run` rather
  than spawning real processes.

```bash
uv run ruff check packages plugins
uv run ruff format packages plugins
```

## ADR workflow

Anything that changes the WebSocket protocol, the public Python API, the
pytest plugin's hook semantics, or the QGIS plugin's contract starts as
an ADR (Architecture Decision Record). The current numbering is:

- ADR-0001: Hub / Worker / WebSocket protocol design
- ADR-0002: pytest plugin design (Locator, auto-wait, expect, fixtures)
- ADR-0003: Test environments (partition model)
- ADR-0004: Inline QGIS spawn helper

ADRs progress through `Proposed` → `Accepted`. Open an issue (or PR
against the ADR file) to propose a new one. Keep ADRs focused on
architecture trade-offs rather than implementation detail; reference
implementations live in code, not in ADRs.

## Commit messages

Conventional-ish prefixes are preferred but not enforced:

- `feat:` new user-visible feature
- `fix:` bug fix
- `refactor:` no behavior change
- `docs:` documentation only
- `test:` test code only
- `ci:` GitHub Actions, build system
- `chore:` version bumps, repo plumbing

Imperative mood, present tense. Body explains *why* when the *what* is
non-obvious. Keep summary lines under ~72 characters.

## Pull requests

- Target the `dev` branch. `main` tracks released versions.
- One logical change per PR. Squash before merge if the PR has commit
  noise.
- All CI matrix cells must be green (Ubuntu + Windows × Python
  3.10/3.11/3.12).
- For new features, include tests. For bug fixes, include a regression
  test that fails before the fix.
- Update `CHANGELOG.md` under `[Unreleased]` if the change is
  user-visible.

## Releasing

The maintainer cuts releases. Process:

1. Bump version in all three places: `packages/qgis-puppeteer/pyproject.toml`,
   `packages/pytest-qgis-puppeteer/pyproject.toml`,
   `plugins/qgis_puppeteer/metadata.txt`.
2. Move `[Unreleased]` notes under a new `[X.Y.Z]` section in `CHANGELOG.md`.
3. Tag `vX.Y.Z` on `main` after merging from `dev`.
4. PyPI publish workflow (TBD) runs on tag.

## License

By contributing, you agree your contributions are licensed under the
license of the package you are touching:

- `packages/qgis-puppeteer/` and `packages/pytest-qgis-puppeteer/`:
  Apache-2.0
- `plugins/qgis_puppeteer/qgis_plugin/`, `plugins/qgis_puppeteer/metadata.txt`
  / `icon.png`, and `plugins/tests/`: GPL-3.0-or-later (the rest of
  `plugins/qgis_puppeteer/` is a copy of the Apache-2.0 library; change it
  under `packages/`)

This dual-license setup exists because the QGIS plugin links against
QGIS at runtime (which is GPL); the standalone Python packages do not.
