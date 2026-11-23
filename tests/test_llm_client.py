"""
Unit tests for StructuredLogAnalyzer using mock OpenAI client responses.
Verifies Pydantic schema validation and tool call payload parsing without live API calls.
"""

import json
import os
from unittest.mock import MagicMock, patch
import httpx
import pytest
from openai import APIConnectionError, APIStatusError
from src.llm_client import StructuredLogAnalyzer
from src.schema import IncidentReport, SeverityLevel


@pytest.fixture
def mock_openai_response():
    """Provides a mocked OpenAI chat completion response returning a tool call payload."""
    mock_response = MagicMock()

    # Valid IncidentReport payload matching Pydantic schema
    sample_payload = {
        "incident_id": "INC-2026-001",
        "title": "Auth Service Database Connection Failure",
        "severity": "HIGH",
        "summary": "Database connectivity dropped for auth-service causing 500 error spikes.",
        "affected_services": ["auth-service", "user-service"],
        "anomalies": [
            {
                "timestamp": "2026-09-04T18:00:00Z",
                "service_name": "auth-service",
                "error_code": "500",
                "message": "DB connection timeout after 5000ms",
                "raw_log_line": "[2026-09-04 18:00:00] ERROR auth-service DB connection timeout",
            }
        ],
        "recommended_actions": [
            "Inspect primary DB host health",
            "Restart connection pool",
        ],
        "requires_escalation": True,
    }

    # Structure mock to match OpenAI's response object model
    mock_tool_call = MagicMock()
    mock_tool_call.function.arguments = json.dumps(sample_payload)

    mock_message = MagicMock()
    mock_message.tool_calls = [mock_tool_call]

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_response.choices = [mock_choice]
    return mock_response


@patch("src.llm_client.OpenAI")
def test_analyze_logs_valid_response(mock_openai_class, mock_openai_response):
    """Verifies that analyze_logs correctly parses and returns a validated IncidentReport object."""
    # Setup mock client instance
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = mock_openai_response
    mock_openai_class.return_value = mock_client_instance

    # Initialize analyzer with a dummy API key
    analyzer = StructuredLogAnalyzer(api_key="sk-dummy-test-key")

    sample_log = "[2026-09-04 18:00:00] ERROR auth-service DB connection timeout"
    report = analyzer.analyze_logs(sample_log)

    # Assertions on validated Pydantic model
    assert isinstance(report, IncidentReport)
    assert report.incident_id == "INC-2026-001"
    assert report.severity == SeverityLevel.HIGH
    assert len(report.anomalies) == 1
    assert report.anomalies[0].service_name == "auth-service"
    assert report.requires_escalation is True


@patch("src.llm_client.OpenAI")
def test_analyze_logs_validation_error(mock_openai_class):
    """Verifies that invalid LLM outputs trigger a ValueError during schema parsing."""
    mock_response = MagicMock()
    mock_tool_call = MagicMock()
    # Invalid payload: missing required fields 'title' and 'severity'
    mock_tool_call.function.arguments = json.dumps({"incident_id": "INC-002"})

    mock_message = MagicMock()
    mock_message.tool_calls = [mock_tool_call]
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]

    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = mock_response
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(api_key="sk-dummy-test-key")

    with pytest.raises(ValueError, match="Failed to validate LLM output"):
        analyzer.analyze_logs("some random log line")


def _text_response(content: str) -> MagicMock:
    """Mock a completion whose report arrived as plain text instead of tool_calls."""
    mock_message = MagicMock()
    mock_message.tool_calls = None
    mock_message.content = content

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_response.choices = [mock_choice]
    return mock_response


@patch("src.llm_client.OpenAI")
def test_ollama_mode_requests_json_schema(mock_openai_class):
    """Ollama mode constrains decoding with the schema, not tool calling."""
    payload = json.dumps(
        {
            "incident_id": "INC-2026-002",
            "title": "Redis connection refused",
            "severity": "HIGH",
            "summary": "Auth gateway rejected tokens after Redis connection refused.",
            "affected_services": ["auth-gateway"],
            "anomalies": [],
            "recommended_actions": ["Restart Redis"],
            "requires_escalation": True,
        }
    )
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = _text_response(payload)
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="llama3.2")
    report = analyzer.analyze_logs(
        "[ERROR] service=auth-gateway redis connection refused"
    )

    assert report.incident_id == "INC-2026-002"
    kwargs = mock_client_instance.chat.completions.create.call_args.kwargs
    assert "tools" not in kwargs
    response_format = kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert "title" in response_format["json_schema"]["schema"]["properties"]


@patch("src.llm_client.OpenAI")
def test_ollama_base_url_is_normalized_to_v1(mock_openai_class):
    """A bare OLLAMA_BASE_URL without /v1 would 404, so the suffix gets appended."""
    with patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://localhost:11434"}):
        StructuredLogAnalyzer(mode="ollama")

    assert mock_openai_class.call_args.kwargs["base_url"] == "http://localhost:11434/v1"


@patch("src.llm_client.OpenAI")
def test_analyze_logs_recovers_fenced_json_report(mock_openai_class):
    """A fenced json report is unwrapped and still validated against the schema."""
    payload = json.dumps(
        {
            "incident_id": "INC-2026-003",
            "title": "Database pool exhausted",
            "severity": "CRITICAL",
            "summary": "Connection pool exhaustion caused cascading 500 errors.",
            "affected_services": ["database-service"],
            "anomalies": [],
            "recommended_actions": ["Raise pool limit"],
            "requires_escalation": True,
        }
    )
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = _text_response(
        f"Here is the report:\n```json\n{payload}\n```"
    )
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="phi4-mini")
    report = analyzer.analyze_logs("[ERROR] db pool exhausted")

    assert report.severity == SeverityLevel.CRITICAL
    assert report.title == "Database pool exhausted"


@patch("src.llm_client.OpenAI")
def test_analyze_logs_recovers_name_arguments_envelope(mock_openai_class):
    """Text shaped as a name/arguments envelope is unwrapped down to the report."""
    payload = {
        "incident_id": "INC-2026-004",
        "title": "Cache service unavailable",
        "severity": "MEDIUM",
        "summary": "Redis cache misses while cache-service was unreachable.",
        "affected_services": ["cache-service"],
        "anomalies": [],
        "recommended_actions": ["Reconnect cache"],
        "requires_escalation": False,
    }
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = _text_response(
        json.dumps({"name": "generate_incident_report", "arguments": payload})
    )
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="qwen2.5-coder:14b")
    report = analyzer.analyze_logs("[ERROR] cache-service unreachable")

    assert report.incident_id == "INC-2026-004"


@patch("src.llm_client.OpenAI")
def test_analyze_logs_refuses_empty_response(mock_openai_class):
    """An empty answer is refused rather than filled in with heuristics."""
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.return_value = _text_response("")
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="llama3.2")

    with pytest.raises(ValueError, match="no generate_incident_report payload"):
        analyzer.analyze_logs("[ERROR] something failed")


OLLAMA_URL = "http://localhost:11434/v1/chat/completions"


def _connection_error() -> APIConnectionError:
    """The endpoint is not answering at all, which reads as a dead server."""
    return APIConnectionError(
        message="Connection error.", request=httpx.Request("POST", OLLAMA_URL)
    )


def _model_missing_error() -> APIStatusError:
    """The endpoint answered, but the requested model is not in its model store."""
    request = httpx.Request("POST", OLLAMA_URL)
    response = httpx.Response(status_code=404, request=request)
    return APIStatusError(
        "Error code: 404 - model 'llama3.2' not found", response=response, body=None
    )


@patch("src.llm_client.OpenAI")
def test_transport_failure_degrades_without_a_model_hint(mock_openai_class):
    """An unreachable endpoint degrades to heuristics and suppresses escalation."""
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.side_effect = _connection_error()
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="llama3.2")
    report = analyzer.analyze_logs("[ERROR] service=database-service timeout 500")

    assert report.requires_escalation is False
    assert report.summary.startswith("[DEGRADED] LLM unavailable (APIConnectionError")
    assert "ollama pull" not in analyzer.degraded_reason


@patch("src.llm_client.OpenAI")
def test_missing_model_404_names_the_fix_in_the_degraded_reason(mock_openai_class):
    """A 404 means the endpoint is alive and the model is absent, so say which to change."""
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create.side_effect = _model_missing_error()
    mock_openai_class.return_value = mock_client_instance

    analyzer = StructuredLogAnalyzer(mode="ollama", model="llama3.2")
    report = analyzer.analyze_logs("[ERROR] service=database-service timeout 500")

    assert report.requires_escalation is False
    assert "ollama pull llama3.2" in analyzer.degraded_reason
    assert "http://localhost:11434/v1" in analyzer.degraded_reason
