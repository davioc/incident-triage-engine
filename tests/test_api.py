"""
Integration tests for FastAPI REST endpoints using TestClient.
"""

import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from src.api import app, routes
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
                raw_log_line="2026-09-04 18:00:01 [ERROR] 502 120ms - api-gateway Bad Gateway"
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
        }
    }

    payload = {
        "raw_logs": "2026-09-04 18:00:01 [ERROR] 502 120ms - api-gateway Bad Gateway",
        "max_context_lines": 20
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
        response = client.post(
            "/api/v1/mode?mode=mock", headers={"HX-Request": "true"}
        )
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