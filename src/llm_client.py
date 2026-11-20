"""
Structured prompt engine and LLM client wrapper.
Enforces Pydantic schema validation on model responses using function/tool calling.

Failure policy:
- Transport/provider failures fall back to the local heuristic analyzer, and the report
  is marked degraded (no escalation) so a fabricated alert is never mistaken for analysis.
- Output that does not validate against IncidentReport is raised, never substituted.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APITimeoutError, APIStatusError, OpenAI
from pydantic import ValidationError

from src.schema import IncidentReport, LogAnomaly, SeverityLevel

logger = logging.getLogger("ai-log-analyzer.llm")

# Provider-side availability problems: safe to degrade. Schema problems are not.
TRANSPORT_ERRORS = (APIConnectionError, APITimeoutError, APIStatusError, OSError)

DEFAULT_ACTIONS = [
    "Inspect log timestamps around initial error spike",
    "Verify downstream dependencies and connection timeouts",
]

IGNORED_KEYS = {
    "host", "ip", "port", "user_id", "status", "latency", "msg", "pid", "env", "timestamp",
}


class StructuredLogAnalyzer:
    """Wrapper around LLM providers to generate structured incident reports from logs."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        mode: Optional[str] = None,
    ):
        # Mode precedence: explicit argument, then TRIAGE_MODE, then inferred from the
        # presence of an API key. Without a key the default is offline mock mode.
        raw_mode = mode or os.getenv("TRIAGE_MODE")
        if raw_mode is None:
            self.mode = "openai" if api_key else "mock"
        else:
            self.mode = raw_mode.lower()

        # Reason the most recent analysis degraded to heuristics, if it did.
        self.degraded_reason: Optional[str] = None

        if self.mode == "ollama":
            # Ollama provides an OpenAI-compatible endpoint
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
            self.client = OpenAI(base_url=base_url, api_key="ollama")

        elif self.mode == "openai":
            self.api_key = api_key or os.getenv("OPENAI_API_KEY")
            if not self.api_key:
                raise ValueError("OPENAI_API_KEY is missing while in openai mode.")
            self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
            self.client = OpenAI(api_key=self.api_key)

        elif self.mode == "mock":
            # Mock mode needs no client, no network and no API key.
            self.model = None

        else:
            raise ValueError(
                f"Unknown TRIAGE_MODE '{self.mode}'. Expected mock, ollama or openai."
            )

    def analyze_logs(
        self, log_batch: str, runbook_context: Optional[str] = None
    ) -> IncidentReport:
        """
        Send a log batch to the LLM and return a response validated against the
        IncidentReport schema. runbook_context, when supplied, grounds the report in the
        team's own remediation docs.
        """
        self.degraded_reason = None

        if self.mode == "mock":
            return self._mock_report(log_batch, runbook_context)

        # -------------------------------------------------------------
        # OLLAMA / OPENAI MODE (LLM API call)
        # -------------------------------------------------------------

        system_prompt = (
            "You are a Senior Site Reliability Engineer (SRE) assisting with incident response. "
            "Analyze the provided log stream, identify root causes, extract critical anomalies, "
            "and output a structured incident report by calling the `generate_incident_report` tool. "
            "Only claim services and causes that the log lines support."
        )

        user_prompt = f"Analyze the following log entries:\n\n{log_batch}"
        if runbook_context:
            user_prompt += (
                "\n\nRelevant internal runbooks (use these for recommended_actions):\n"
                f"{runbook_context}"
            )

        tool_definition = {
            "type": "function",
            "function": {
                "name": "generate_incident_report",
                "description": "Generates a structured SRE incident report from raw or parsed log batches.",
                "parameters": IncidentReport.model_json_schema(),
            },
        }

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[tool_definition],
                tool_choice={"type": "function", "function": {"name": "generate_incident_report"}},
                temperature=0.1,  # Low temperature for deterministic analysis
            )
        except TRANSPORT_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("LLM call failed (%s). Falling back to local heuristic analysis.", reason)
            report = self._mock_report(log_batch, runbook_context)
            report.summary = f"[DEGRADED] LLM unavailable ({reason}). {report.summary}"
            report.requires_escalation = False
            self.degraded_reason = reason
            return report

        # The model answered; parse and validate its tool call.
        tool_calls = getattr(response.choices[0].message, "tool_calls", None)
        if not tool_calls:
            raise ValueError(
                "LLM response contained no generate_incident_report tool call; refusing to guess."
            )

        try:
            parsed_data = json.loads(tool_calls[0].function.arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to validate LLM output: tool call arguments are not JSON ({exc})") from exc

        try:
            return IncidentReport.model_validate(parsed_data)
        except ValidationError as exc:
            raise ValueError(f"Failed to validate LLM output: {exc}") from exc

    def _mock_report(
        self, log_batch: str, runbook_context: Optional[str] = None
    ) -> IncidentReport:
        """Offline heuristic extraction: zero API calls, used for mock mode and degradation."""
        lines = [line.strip() for line in log_batch.strip().splitlines() if line.strip()]

        extracted_anomalies: List[Dict[str, Any]] = []
        services_found: set[str] = set()

        for line in lines:
            service = "core-service"
            lower_line = line.lower()

            # 1. Prefer explicit key-value parameters (e.g. service=auth)
            for part in line.split():
                if "=" in part:
                    key = part.split("=")[0].lower()
                    if key not in IGNORED_KEYS:
                        service = key
                        break

            # 2. Fall back to keyword heuristics for plain messages
            if service == "core-service":
                if any(k in lower_line for k in ("database", "db", "pool")):
                    service = "database-service"
                elif any(k in lower_line for k in ("redis", "cache")):
                    service = "cache-service"
                elif any(k in lower_line for k in ("rate limiter", "user_id")):
                    service = "auth-gateway"
                elif any(k in lower_line for k in ("render", "dashboard", "template")):
                    service = "frontend-service"

            services_found.add(service)

            error_code = next(
                (code for code in ("500", "503", "429", "404", "400") if code in line), "ERR"
            )

            extracted_anomalies.append(
                LogAnomaly(
                    timestamp=line.split()[0] if line else "unknown",
                    service_name=service,
                    error_code=error_code,
                    message=line[:120],
                    raw_log_line=line,
                )
            )

        error_lines = [line for line in lines if "[ERROR]" in line or "status=5" in line]
        severity = SeverityLevel.HIGH if error_lines else SeverityLevel.MEDIUM

        actions = list(DEFAULT_ACTIONS)
        if runbook_context:
            for heading in runbook_context.splitlines():
                if heading.startswith("--- Runbook:"):
                    actions.insert(0, f"Follow runbook: {heading.removeprefix('--- Runbook:').rstrip(' -')}")

        return IncidentReport(
            incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
            title=f"Detected Anomalies in {', '.join(sorted(services_found)) or 'System Log Stream'}",
            severity=severity,
            summary=f"[MOCK MODE] Processed {len(lines)} log lines locally.",
            affected_services=sorted(services_found) or ["app-service"],
            anomalies=extracted_anomalies[:5],
            recommended_actions=actions,
            requires_escalation=True,
        )
