"""Dashboard route tests: the page the browser gets, not just the API.

The dashboard is served by src/main.py, which mounts the same router as src/api, so the
only thing to check here is the substitution the HTML file relies on.
"""

from fastapi.testclient import TestClient
from src.config import settings
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
