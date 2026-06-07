"""QGIS profile startup hook that enables coverage.py subprocess measurement.

Recommended over the site-wide ``.pth`` file because it is scoped to a single
QGIS *profile* (e.g. your dedicated ``--profile=test``) and does not touch the
QGIS Python's ``site-packages``.

Install — copy this file to your test profile's python startup path:

    Windows:  %APPDATA%\\QGIS\\QGIS3\\profiles\\<profile>\\python\\startup.py
    Linux:    ~/.local/share/QGIS/QGIS3/profiles/<profile>/python/startup.py
    macOS:    ~/Library/Application Support/QGIS/QGIS3/profiles/<profile>/python/startup.py

It is a no-op unless ``COVERAGE_PROCESS_START`` is set in the environment, so
normal (non-coverage) QGIS runs are unaffected. qgis-puppeteer propagates the
pytest process environment to the spawned QGIS, so exporting
``COVERAGE_PROCESS_START`` (pointing at your ``.coveragerc``) before running
pytest is enough to switch this on.

Note: ``coverage`` must be importable in the QGIS-bundled Python
(``<qgis-python> -m pip install coverage``).
"""

import os

if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage

        coverage.process_startup()
    except Exception as exc:  # never block QGIS startup over coverage
        import sys

        print(f"[coverage] startup hook failed: {exc!r}", file=sys.stderr)
