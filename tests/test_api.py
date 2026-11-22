"""
Integration tests for FastAPI REST endpoints using TestClient.
"""

import os
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
        response = client.post("/api/v1/mode?mode=ollama&model=llama3.2")
        assert response.status_code == 200
        state = response.json()
        assert state["mode"] == "ollama"
        assert state["model"] == "llama3.2"
        assert state["base_url"].endswith("/v1")
    finally:
        routes.engine.analyzer = original


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
