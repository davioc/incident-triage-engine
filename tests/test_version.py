"""The version is defined once; a copy elsewhere is a drift bug waiting to happen.

Bumping Settings.VERSION must update /docs, the dashboard badge and /api/v1/health at the
same time. Anything that hardcodes the number instead of reading it would silently claim an
older release, so these tests fail when a copy appears.
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from src.api import app
from src.config import settings
from src.main import app as dashboard_app

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
# A version number, not a dotted IP: "0.0.0.0" and "127.0.0.1" must not look like a copy.
VERSION_PATTERN = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")


def test_python_source_defines_the_version_in_one_place():
    offenders = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path == SRC_DIR / "config.py":
            continue  # the single definition
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if VERSION_PATTERN.search(line):
                offenders.append(
                    f"{path.relative_to(SRC_DIR)}:{line_number}: {line.strip()}"
                )

    assert (
        not offenders
    ), "hardcoded version literal, read settings.VERSION instead: " + "\n".join(
        offenders
    )


def test_dashboard_html_carries_no_copy_of_the_version():
    html = (SRC_DIR / "web" / "index.html").read_text(encoding="utf-8")
    assert "{{VERSION}}" in html
    assert settings.VERSION not in html


def test_both_apps_and_health_report_the_same_version():
    assert app.version == settings.VERSION
    assert dashboard_app.version == settings.VERSION
    assert TestClient(app).get("/api/v1/health").json()["version"] == settings.VERSION
