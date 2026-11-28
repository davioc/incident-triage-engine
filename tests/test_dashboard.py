"""Dashboard route tests: the page the browser gets, not just the API.

The dashboard is served by src/main.py, which mounts the same router as src/api, so the
only thing to check here is the substitution the HTML file relies on.
"""

import re

from fastapi.testclient import TestClient
from src.config import settings
from src.log_parser import parse_log_line
from src.main import app

client = TestClient(app)


def test_dashboard_renders_with_badge_version():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    html = response.text
    # Nothing is left to substitute: a stray placeholder would render as literal text.
    assert "{{VERSION}}" not in html
    assert f"v{settings.VERSION}" in html


def test_dashboard_keeps_the_triage_controls():
    html = client.get("/").text
    assert "/api/v1/triage" in html
    assert "Analyze" in html


def test_dashboard_wires_the_stored_report_notice():
    """The notice fills itself from /api/v1/reports/summary on load and after a triage,
    so the page must carry the hook rather than a hardcoded count."""
    html = client.get("/").text
    assert 'id="storage-status"' in html
    assert 'hx-get="/api/v1/reports/summary"' in html
    assert "storage-refresh" in html


def test_dashboard_sample_logs_all_parse():
    """The sample burst is the project's own example, so pressing the button must load
    lines the parser accepts. parse_log_line anchors at the start of the line and
    process_raw_logs strips only the whole string, so indented lines silently count as
    unparseable — which is what this guards against."""
    html = client.get("/").text
    block = html.split("function loadSampleLogs")[1]
    block = block.split("getElementById('raw_logs')")[0]
    lines = re.findall(r"'(\d{4}-\d{2}-\d{2}[^']*)'", block)

    assert len(lines) == 5
    entries = [parse_log_line(line) for line in lines]
    unparseable = [line for line, entry in zip(lines, entries) if entry is None]
    assert not unparseable, f"sample lines the parser rejects: {unparseable}"

    # Four of the five carry a 4xx/5xx status, which is what the engine filters on.
    assert sum(1 for entry in entries if entry["status"] >= 400) == 4
