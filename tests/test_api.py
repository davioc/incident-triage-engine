"""
Integration tests for FastAPI REST endpoints using TestClient.
"""

import os
import socket
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.api import app, routes
from src.config import settings
from src.db import Base, get_engine, reset_engine, save_report
from src.llm_client import StructuredLogAnalyzer
from src.schema import IncidentReport, SeverityLevel, LogAnomaly

client = TestClient(app)


def test_health_check_endpoint():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_health_names_the_process_that_answered():
    # A dev machine can run the container and a host server at the same time, so the
    # response has to say who produced it. Hostname and pid are the identifiers; the
    # address records what the caller addressed the request to.
    server = client.get("/api/v1/health").json()["server"]
    assert server["hostname"] == socket.gethostname()
    assert server["pid"] == os.getpid()
    assert server["address"]


def test_health_records_the_address_the_request_was_sent_to():
    response = client.get("/api/v1/health", headers={"Host": "127.0.0.1:8001"})
    assert response.json()["server"]["address"] == "127.0.0.1:8001"


@patch("src.api.routes.engine")
def test_triage_endpoint_success(mock_engine):
    # Mock engine execution to keep test offline and fast
    mock_report = IncidentReport(
        incident_id="INC-2026-TEST",
        title="API Gateway High Error Rate",
        severity=SeverityLevel.HIGH,
        summary="Upstream error rate exceeded 5% threshold.",
        affected_services=["api-gateway"],
        anomalies=[
            LogAnomaly(
                timestamp="2026-09-04 18:00:01",
                service_name="api-gateway",
                error_code="502",
                message="Bad Gateway",
                raw_log_line="2026-09-04 18:00:01 [ERROR] 502 120ms - api-gateway Bad Gateway",
            )
        ],
        recommended_actions=["Check upstream service health"],
        requires_escalation=True,
    )

    # Fixed typo: process_raw_logs (with underscore)
    mock_engine.process_raw_logs.return_value = {
        "incident_report": mock_report.model_dump(),
        "notification_payload": {"channel": "#alerts-critical"},
        "metadata": {
            "total_logs_ingested": 10,
            "error_logs_analyzed": 2,
            "requires_escalation": True,
        },
    }

    payload = {
        "raw_logs": "2026-09-04 18:00:01 [ERROR] 502 120ms - api-gateway Bad Gateway",
        "max_context_lines": 20,
    }

    response = client.post("/api/v1/triage", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["incident_report"]["incident_id"] == "INC-2026-TEST"
    assert data["metadata"]["requires_escalation"] is True


def test_triage_endpoint_empty_logs():
    payload = {"raw_logs": "   "}  # Fails min_length=10 Pydantic validation
    response = client.post("/api/v1/triage", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_short"


# --- Runtime backend switching ---------------------------------------------------


def restore_backend():
    """Put the shared engine's analyzer back after a switch changes it."""
    return routes.engine.analyzer


def test_mode_endpoint_reports_active_backend():
    response = client.get("/api/v1/mode")
    assert response.status_code == 200
    assert response.json()["mode"] in {"mock", "ollama", "openai"}


def test_mode_switch_to_mock_is_process_local():
    original = restore_backend()
    try:
        response = client.post("/api/v1/mode?mode=mock")
        assert response.status_code == 200
        assert response.json()["mode"] == "mock"
        assert routes.engine.analyzer.mode == "mock"
        assert routes.engine.analyzer.model is None
    finally:
        routes.engine.analyzer = original


def test_mode_switch_names_ollama_model_and_endpoint():
    original = restore_backend()
    try:
        with patch("src.api.routes.ollama_model_warning", return_value=None):
            response = client.post("/api/v1/mode?mode=ollama&model=llama3.2")
        assert response.status_code == 200
        state = response.json()
        assert state["mode"] == "ollama"
        assert state["model"] == "llama3.2"
        assert state["base_url"].endswith("/v1")
        assert state["warning"] is None
    finally:
        routes.engine.analyzer = original
        routes._warning_state["key"] = None
        routes._warning_state["text"] = None


def test_mode_switch_rejects_unknown_mode():
    assert client.post("/api/v1/mode?mode=anthropic").status_code == 400


def test_mode_switch_openai_without_key_keeps_previous_backend():
    original = restore_backend()
    try:
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            response = client.post("/api/v1/mode?mode=openai")
        assert response.status_code == 400
        assert "OPENAI_API_KEY" in response.json()["detail"]
        assert routes.engine.analyzer is original
    finally:
        routes.engine.analyzer = original


class _FakeModel:
    def __init__(self, model_id):
        self.id = model_id


class _FakeClient:
    """Stand-in for the OpenAI client's model listing, so the probe stays offline."""

    def __init__(self, ids=None, error=None):
        self.models = self
        self._ids = ids if ids is not None else []
        self._error = error

    def list(self, timeout=None):
        if self._error:
            raise self._error
        return [_FakeModel(model_id) for model_id in self._ids]


class _FakeAnalyzer:
    def __init__(self, mode, model, client):
        self.mode = mode
        self.model = model
        self.base_url = "http://localhost:11434/v1"
        self.client = client


def test_model_probe_names_the_missing_model_and_lists_what_exists():
    analyzer = _FakeAnalyzer("ollama", "llama3.2", _FakeClient(["phi4-mini:latest"]))
    warning = routes.ollama_model_warning(analyzer)
    assert warning is not None
    assert "llama3.2" in warning
    assert "phi4-mini:latest" in warning
    assert "ollama pull llama3.2" in warning


def test_model_probe_accepts_a_bare_name_that_resolves_to_latest():
    analyzer = _FakeAnalyzer("ollama", "llama3.2", _FakeClient(["llama3.2:latest"]))
    assert routes.ollama_model_warning(analyzer) is None


def test_model_probe_unreadable_endpoint_is_a_warning_not_an_error():
    analyzer = _FakeAnalyzer(
        "ollama", "llama3.2", _FakeClient(error=OSError("no route"))
    )
    warning = routes.ollama_model_warning(analyzer)
    assert warning is not None
    assert "Could not read the model list" in warning
    assert "http://localhost:11434/v1" in warning


def test_model_probe_empty_endpoint_names_nothing_pulled():
    analyzer = _FakeAnalyzer("ollama", "llama3.2", _FakeClient([]))
    warning = routes.ollama_model_warning(analyzer)
    assert "no models pulled on it" in warning


def test_model_probe_only_applies_to_ollama():
    assert (
        routes.ollama_model_warning(_FakeAnalyzer("openai", "gpt-4o", _FakeClient([])))
        is None
    )
    assert (
        routes.ollama_model_warning(_FakeAnalyzer("mock", None, _FakeClient([])))
        is None
    )


def test_mode_switch_surfaces_the_model_warning_and_keeps_it_until_next_switch():
    original = restore_backend()
    try:
        with patch(
            "src.api.routes.ollama_model_warning",
            return_value="Model 'x' is not there.",
        ):
            response = client.post("/api/v1/mode?mode=ollama&model=x")
        assert response.status_code == 200
        assert response.json()["warning"] == "Model 'x' is not there."
        # The menu fragment renders it too, which is where the dashboard shows it.
        with patch(
            "src.api.routes.ollama_model_warning",
            return_value="Model 'x' is not there.",
        ):
            fragment = client.post(
                "/api/v1/mode?mode=ollama&model=x", headers={"HX-Request": "true"}
            )
        assert "Model &#x27;x&#x27; is not there." in fragment.text
        assert client.get("/api/v1/mode").json()["warning"] == "Model 'x' is not there."

        # A later switch replaces the warning, so a healthy backend stops showing one.
        with patch("src.api.routes.ollama_model_warning", return_value=None):
            client.post("/api/v1/mode?mode=mock")
        assert client.get("/api/v1/mode").json()["warning"] is None
    finally:
        routes.engine.analyzer = original
        routes._warning_state["key"] = None
        routes._warning_state["text"] = None


def test_mode_endpoint_htmx_returns_menu_fragment():
    original = restore_backend()
    try:
        response = client.post("/api/v1/mode?mode=mock", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "mode-control" in response.text
        assert "Serving" in response.text
    finally:
        routes.engine.analyzer = original


def test_triage_metadata_names_active_backend():
    original_analyzer = restore_backend()
    original_runbooks = routes.engine.use_runbooks
    try:
        # Runbook retrieval loads an embedding model; this test only checks metadata.
        routes.engine.use_runbooks = False
        routes.engine.analyzer = StructuredLogAnalyzer(mode="mock")

        response = client.post(
            "/api/v1/triage",
            json={
                "raw_logs": "2026-09-04 18:00:01 [ERROR] 500 120ms - db pool timeout",
                "max_context_lines": 20,
            },
        )
        assert response.status_code == 200
        metadata = response.json()["metadata"]
        assert metadata["analysis_mode"] == "mock"
        assert metadata["model"] is None
    finally:
        routes.engine.analyzer = original_analyzer
        routes.engine.use_runbooks = original_runbooks


# --- Report persistence ------------------------------------------------------------

MEMORY_URL = "sqlite:///:memory:"
# A path SQLite cannot open: storage is configured but unusable, and the failure is
# local and instant, so the suite never opens a socket.
UNREACHABLE_URL = "sqlite:///no_such_dir/nested.db"


@pytest.fixture()
def storage():
    """Enabled storage backed by SQLite, restored afterwards."""
    original = settings.DATABASE_URL
    settings.DATABASE_URL = MEMORY_URL
    reset_engine()
    Base.metadata.create_all(get_engine())
    yield MEMORY_URL
    settings.DATABASE_URL = original
    reset_engine()


def stub_triage_result():
    return {
        "incident_report": {
            "incident_id": "INC-2026-TEST",
            "title": "API Gateway High Error Rate",
            "severity": "HIGH",
            "summary": "Upstream error rate exceeded 5% threshold.",
            "affected_services": ["api-gateway"],
            "anomalies": [],
            "recommended_actions": ["Check upstream service health"],
            "requires_escalation": True,
        },
        "notification_payload": {"channel": "#alerts-critical"},
        "metadata": {
            "total_logs_ingested": 10,
            "error_logs_analyzed": 2,
            "unparseable_lines": 1,
            "requires_escalation": True,
            "analysis_mode": "mock",
            "model": None,
            "degraded": None,
        },
    }


def test_health_reports_storage_disabled_by_default():
    storage_state = client.get("/api/v1/health").json()["storage"]
    assert storage_state["enabled"] is False
    assert "DATABASE_URL" in storage_state["reason"]


def test_health_reports_configured_storage(storage):
    storage_state = client.get("/api/v1/health").json()["storage"]
    assert storage_state["enabled"] is True
    assert storage_state["backend"] == "sqlite"
    assert storage_state["table"] == "triage_reports"


def test_reports_endpoints_explain_disabled_storage():
    assert client.get("/api/v1/reports").status_code == 503
    assert "DATABASE_URL" in client.get("/api/v1/reports").json()["detail"]
    assert client.get("/api/v1/reports/some-id").status_code == 503
    assert client.delete("/api/v1/reports/some-id").status_code == 503


@patch("src.api.routes.engine")
def test_triage_stores_the_report_then_reads_it_back(mock_engine, storage):
    mock_engine.process_raw_logs.return_value = stub_triage_result()

    response = client.post(
        "/api/v1/triage",
        json={
            "raw_logs": "2026-09-04 18:00:01 [ERROR] 502 120ms - gateway Bad Gateway"
        },
    )
    assert response.status_code == 200
    stored = response.json()["metadata"]["storage"]
    assert stored["enabled"] is True
    assert stored["stored"] is True
    assert stored["report_id"]

    listing = client.get("/api/v1/reports").json()
    assert listing["storage"]["enabled"] is True
    assert [row["incident_id"] for row in listing["reports"]] == ["INC-2026-TEST"]
    assert listing["reports"][0]["severity"] == "HIGH"

    report = client.get(f"/api/v1/reports/{stored['report_id']}")
    assert report.status_code == 200
    body = report.json()
    assert body["summary"] == "Upstream error rate exceeded 5% threshold."
    assert body["analysis_mode"] == "mock"
    assert body["unparseable_lines"] == 1

    assert client.delete(f"/api/v1/reports/{stored['report_id']}").status_code == 200
    assert client.get(f"/api/v1/reports/{stored['report_id']}").status_code == 404


def test_report_listing_rejects_an_unknown_severity(storage):
    response = client.get("/api/v1/reports?severity=URGENT")
    assert response.status_code == 400
    assert "Unknown severity" in response.json()["detail"]


def test_report_listing_filters_by_severity(storage):
    save_report(
        {
            "incident_report": {
                **stub_triage_result()["incident_report"],
                "incident_id": "INC-critical",
                "severity": "CRITICAL",
            },
            "metadata": {"analysis_mode": "mock"},
        }
    )
    save_report(stub_triage_result())

    rows = client.get("/api/v1/reports?severity=CRITICAL").json()["reports"]
    assert [row["incident_id"] for row in rows] == ["INC-critical"]


def test_reports_endpoints_report_an_unreachable_database():
    original = settings.DATABASE_URL
    settings.DATABASE_URL = UNREACHABLE_URL
    reset_engine()
    try:
        response = client.get("/api/v1/reports")
        assert response.status_code == 503
        assert "unreachable" in response.json()["detail"]
    finally:
        settings.DATABASE_URL = original
        reset_engine()


@patch("src.api.routes.engine")
def test_triage_survives_a_storage_failure(mock_engine):
    """A database that cannot be written to must not change the analysis result."""
    original = settings.DATABASE_URL
    settings.DATABASE_URL = UNREACHABLE_URL
    reset_engine()
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    try:
        response = client.post(
            "/api/v1/triage",
            json={
                "raw_logs": "2026-09-04 18:00:01 [ERROR] 502 120ms - gateway Bad Gateway"
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["incident_report"]["incident_id"] == "INC-2026-TEST"
        storage = body["metadata"]["storage"]
        assert storage["enabled"] is True
        assert storage["stored"] is False
        assert storage["error"]
    finally:
        settings.DATABASE_URL = original
        reset_engine()


# --- Provenance on the dashboard fragment -------------------------------------------

DASHBOARD_HEADERS = {"HX-Request": "true", "HX-Current-URL": "http://localhost:8000/"}
SAMPLE_LOGS = "2026-09-04 18:00:01 [ERROR] 502 120ms - gateway Bad Gateway"


@patch("src.api.routes.engine")
def test_dashboard_fragment_links_the_stored_report(mock_engine, storage):
    """The report id is only useful on the dashboard if the fragment carries it."""
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    fragment = client.post(
        "/api/v1/triage", json={"raw_logs": SAMPLE_LOGS}, headers=DASHBOARD_HEADERS
    )
    assert fragment.status_code == 200

    stored_id = client.get("/api/v1/reports").json()["reports"][0]["id"]
    assert f'href="/api/v1/reports/{stored_id}"' in fragment.text
    assert "Stored" in fragment.text
    assert "Analysis: mock" in fragment.text


@patch("src.api.routes.engine")
def test_dashboard_fragment_names_why_nothing_was_stored(mock_engine):
    """Disabled storage is a reason, not an absence: the fragment has to say so."""
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    fragment = client.post(
        "/api/v1/triage", json={"raw_logs": SAMPLE_LOGS}, headers=DASHBOARD_HEADERS
    )
    assert "Not stored" in fragment.text
    assert "DATABASE_URL" in fragment.text
    assert client.get("/api/v1/reports").status_code == 503


@patch("src.api.routes.engine")
def test_dashboard_fragment_names_a_storage_failure(mock_engine):
    """A configured database that cannot be written to still returns the analysis."""
    original = settings.DATABASE_URL
    settings.DATABASE_URL = UNREACHABLE_URL
    reset_engine()
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    try:
        fragment = client.post(
            "/api/v1/triage", json={"raw_logs": SAMPLE_LOGS}, headers=DASHBOARD_HEADERS
        )
        assert fragment.status_code == 200
        assert "Not stored" in fragment.text
    finally:
        settings.DATABASE_URL = original
        reset_engine()


@patch("src.api.routes.engine")
def test_json_triage_reports_the_disabled_storage_reason(mock_engine):
    """JSON consumers get the same reason the dashboard shows."""
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    body = client.post("/api/v1/triage", json={"raw_logs": SAMPLE_LOGS}).json()
    assert body["metadata"]["storage"] == {
        "enabled": False,
        "stored": False,
        "reason": "DATABASE_URL is not set; report storage is disabled",
    }


# --- Stored-report notice ------------------------------------------------------------


def degraded_triage_result(incident_id: str) -> Dict[str, Any]:
    result = stub_triage_result()
    result["incident_report"]["incident_id"] = incident_id
    result["metadata"]["degraded"] = "provider request failed; heuristic triage applied"
    return result


def test_summary_names_disabled_storage_without_failing():
    """The notice has to answer, not fail: disabled storage is a state to display."""
    response = client.get("/api/v1/reports/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["total"] == 0
    assert "DATABASE_URL" in body["storage"]["reason"]
    # The listing keeps 503, so 'not configured' stays distinct from 'nothing stored'.
    assert client.get("/api/v1/reports").status_code == 503


def test_summary_counts_rows_and_separates_degraded_ones(storage):
    save_report(
        {
            "incident_report": {
                **stub_triage_result()["incident_report"],
                "incident_id": "INC-critical",
                "severity": "CRITICAL",
            },
            "metadata": {"analysis_mode": "ollama", "model": "qwen2.5-coder"},
        }
    )
    save_report(degraded_triage_result("INC-degraded"))

    body = client.get("/api/v1/reports/summary").json()
    assert body["enabled"] is True
    assert body["total"] == 2
    assert body["degraded"] == 1
    # Newest first, matching the listing: the notice is a preview, not a digest.
    assert [row["incident_id"] for row in body["reports"]] == [
        "INC-degraded",
        "INC-critical",
    ]
    assert body["reports"][1]["degraded"] is None

    limited = client.get("/api/v1/reports/summary?limit=1").json()
    assert len(limited["reports"]) == 1
    assert limited["total"] == 2


def test_summary_fragment_renders_the_notice(storage):
    save_report(
        {
            "incident_report": {
                **stub_triage_result()["incident_report"],
                "incident_id": "INC-critical",
                "severity": "CRITICAL",
            },
            "metadata": {"analysis_mode": "ollama", "model": "qwen2.5-coder"},
        }
    )
    save_report(degraded_triage_result("INC-degraded"))

    stored_id = client.get("/api/v1/reports").json()["reports"][0]["id"]
    fragment = client.get("/api/v1/reports/summary", headers=DASHBOARD_HEADERS)
    assert fragment.status_code == 200
    assert 'id="storage-status"' in fragment.text
    assert "2 stored" in fragment.text
    assert "1 degraded" in fragment.text
    assert "CRITICAL" in fragment.text
    assert "qwen2.5-coder" in fragment.text
    assert f'href="/api/v1/reports/{stored_id}"' in fragment.text


def test_summary_fragment_names_disabled_storage():
    fragment = client.get("/api/v1/reports/summary", headers=DASHBOARD_HEADERS)
    assert fragment.status_code == 200
    assert "No report history" in fragment.text
    assert "DATABASE_URL" in fragment.text


def test_summary_fragment_says_nothing_is_stored_yet(storage):
    fragment = client.get("/api/v1/reports/summary", headers=DASHBOARD_HEADERS)
    assert "0 stored" in fragment.text
    assert "Nothing stored yet" in fragment.text


def test_summary_reports_an_unreachable_database():
    """Configured but unusable storage is a failure, so it does keep 503."""
    original = settings.DATABASE_URL
    settings.DATABASE_URL = UNREACHABLE_URL
    reset_engine()
    try:
        response = client.get("/api/v1/reports/summary")
        assert response.status_code == 503
        assert "unreachable" in response.json()["detail"]
    finally:
        settings.DATABASE_URL = original
        reset_engine()


@patch("src.api.routes.engine")
def test_triage_refreshes_the_notice_with_the_row_it_just_wrote(mock_engine, storage):
    """The dashboard refreshes the notice after an analysis, so the new row appears."""
    mock_engine.process_raw_logs.return_value = stub_triage_result()
    before = client.get("/api/v1/reports/summary").json()["total"]

    client.post("/api/v1/triage", json={"raw_logs": SAMPLE_LOGS})

    after = client.get("/api/v1/reports/summary").json()
    assert after["total"] == before + 1
    assert after["degraded"] == 0
    assert after["reports"][0]["incident_id"] == "INC-2026-TEST"
