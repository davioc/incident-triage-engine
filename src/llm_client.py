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
import re
import uuid
from typing import Any, Dict, List, Optional

from openai import APIConnectionError, APITimeoutError, APIStatusError, OpenAI
from pydantic import ValidationError

from src.schema import IncidentReport, LogAnomaly, SeverityLevel

logger = logging.getLogger("incident-triage-engine.llm")

# Provider-side availability problems: safe to degrade. Schema problems are not.
TRANSPORT_ERRORS = (APIConnectionError, APITimeoutError, APIStatusError, OSError)


def _failure_hint(
    exc: Exception, mode: str, base_url: Optional[str], model: Optional[str]
) -> str:
    """
    A 404 from Ollama means the endpoint is answering but the model is not there, which is a
    configuration mistake rather than an outage. Say so in the degraded reason, because
    'Connection error.'-style wording sends people looking for a dead server.
    """
    if mode == "ollama" and getattr(exc, "status_code", None) == 404:
        return (
            f"Model '{model}' is not available at {base_url}. Pull it on that host "
            f"(ollama pull {model}) or point OLLAMA_BASE_URL at an Ollama that has it."
        )
    return ""


REPORT_TOOL_NAME = "generate_incident_report"

DEFAULT_ACTIONS = [
    "Inspect log timestamps around initial error spike",
    "Verify downstream dependencies and connection timeouts",
]

IGNORED_KEYS = {
    "host",
    "ip",
    "port",
    "user_id",
    "status",
    "latency",
    "msg",
    "pid",
    "env",
    "timestamp",
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
        # Endpoint actually in use, for the dashboard's Serving menu.
        self.base_url: Optional[str] = None

        if self.mode == "ollama":
            # Ollama provides an OpenAI-compatible endpoint, which lives under /v1.
            # A bare http://localhost:11434 in OLLAMA_BASE_URL would 404, so append it.
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            if not base_url.rstrip("/").endswith("/v1"):
                base_url = base_url.rstrip("/") + "/v1"
            self.model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
            self.base_url = base_url
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

        schema = IncidentReport.model_json_schema()

        if self.mode == "ollama":
            # Ollama does not reliably fill the structured ``tool_calls`` field for local
            # models: it writes the call into plain text and sometimes omits required
            # fields. Supplying the JSON schema instead constrains decoding to that
            # schema, so every required field is emitted.
            system_prompt = (
                "You are a Senior Site Reliability Engineer (SRE) assisting with incident response. "
                "Analyze the provided log stream, identify root causes, extract critical anomalies, "
                f"and respond with a single JSON object matching the `{REPORT_TOOL_NAME}` schema. "
                "Only claim services and causes that the log lines support."
            )
            request_kwargs = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": REPORT_TOOL_NAME,
                        "schema": schema,
                        "strict": True,
                    },
                }
            }
        else:
            system_prompt = (
                "You are a Senior Site Reliability Engineer (SRE) assisting with incident response. "
                "Analyze the provided log stream, identify root causes, extract critical anomalies, "
                f"and output a structured incident report by calling the `{REPORT_TOOL_NAME}` tool. "
                "Only claim services and causes that the log lines support."
            )
            request_kwargs = {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": REPORT_TOOL_NAME,
                            "description": "Generates a structured SRE incident report from raw or parsed log batches.",
                            "parameters": schema,
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": REPORT_TOOL_NAME},
                },
            }

        user_prompt = f"Analyze the following log entries:\n\n{log_batch}"
        if runbook_context:
            user_prompt += (
                "\n\nRelevant internal runbooks (use these for recommended_actions):\n"
                f"{runbook_context}"
            )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,  # Low temperature for deterministic analysis
                **request_kwargs,
            )
        except TRANSPORT_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            hint = _failure_hint(exc, self.mode, self.base_url, self.model)
            if hint:
                reason = f"{reason} | {hint}"
            logger.warning(
                "LLM call failed (%s). Falling back to local heuristic analysis.",
                reason,
            )
            report = self._mock_report(log_batch, runbook_context)
            report.summary = f"[DEGRADED] LLM unavailable ({reason}). {report.summary}"
            report.requires_escalation = False
            self.degraded_reason = reason
            return report

        # The model answered; parse and validate its report payload.
        tool_calls = getattr(response.choices[0].message, "tool_calls", None)
        if tool_calls:
            raw = tool_calls[0].function.arguments
        else:
            raw = self._tool_payload_from_text(response.choices[0].message)

        try:
            parsed_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to validate LLM output: report payload is not JSON ({exc})"
            ) from exc

        # Some providers wrap the arguments in a {"name", "arguments"} envelope.
        if isinstance(parsed_data, dict) and "arguments" in parsed_data:
            parsed_data = parsed_data["arguments"]

        try:
            return IncidentReport.model_validate(parsed_data)
        except ValidationError as exc:
            raise ValueError(f"Failed to validate LLM output: {exc}") from exc

    @staticmethod
    def _tool_payload_from_text(message: Any) -> str:
        """
        A model that did not fill ``tool_calls`` puts the report in plain text instead:
        bare JSON, a fenced ```json block, an ``<tools>`` block, or a ``{"name",
        "arguments"}`` envelope. Recover the JSON from those shapes. This only unwraps the
        envelope — the payload is still validated against IncidentReport, so output that
        does not match the schema raises instead of being accepted.
        """
        text = (getattr(message, "content", None) or "").strip()
        if not text:
            raise ValueError(
                f"LLM response contained no {REPORT_TOOL_NAME} payload; refusing to guess."
            )

        # Strip an <tools>...</tools> wrapper if present.
        if text.startswith("<tools>") and text.endswith("</tools>"):
            text = text[len("<tools>") : -len("</tools>")].strip()

        # A fenced block anywhere in the reply is the payload; prose around it is noise.
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        elif not text.startswith("{"):
            # Prose wrapping a single JSON object: take the widest brace span present.
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError(
                    f"LLM response contained no {REPORT_TOOL_NAME} payload; refusing to guess."
                )
            text = text[start : end + 1]

        return text

    def _mock_report(
        self, log_batch: str, runbook_context: Optional[str] = None
    ) -> IncidentReport:
        """Offline heuristic extraction: zero API calls, used for mock mode and degradation."""
        lines = [
            line.strip() for line in log_batch.strip().splitlines() if line.strip()
        ]

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
                (code for code in ("500", "503", "429", "404", "400") if code in line),
                "ERR",
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

        error_lines = [
            line for line in lines if "[ERROR]" in line or "status=5" in line
        ]
        severity = SeverityLevel.HIGH if error_lines else SeverityLevel.MEDIUM

        actions = list(DEFAULT_ACTIONS)
        if runbook_context:
            for heading in runbook_context.splitlines():
                if heading.startswith("--- Runbook:"):
                    action = (
                        heading.removeprefix("--- Runbook:").strip().rstrip("-").strip()
                    )
                    actions.insert(0, f"Follow runbook: {action}")

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
