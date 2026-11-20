"""
HTTP layer for the triage service: the canonical /api/v1 contract plus HTMX
fragment rendering for the dashboard. Both app objects (src/api/__init__.py for the
API, src/main.py for the dashboard) mount this router, so there is exactly one
implementation of each endpoint.
"""

import html
import logging
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from src.engine import IncidentTriageEngine
from src.schema import IncidentReport

logger = logging.getLogger("incident-triage-engine.api")

router = APIRouter()

# Runbook retrieval is enabled for the served app; tests and the CLI leave it off.
engine = IncidentTriageEngine(use_runbooks=True)


class TriageRequest(BaseModel):
    """Payload sent by the web client or a webhook containing raw telemetry logs."""

    raw_logs: str = Field(
        ...,
        min_length=10,
        description="Raw string stream containing telemetry log lines.",
        json_schema_extra={
            "examples": [
                "2026-09-04 18:00:01 [ERROR] 500 5200ms - auth-service DB connection timeout"
            ]
        },
    )
    max_context_lines: Optional[int] = Field(
        default=50,
        ge=5,
        le=500,
        description="Maximum lines to preserve in the compressed context window.",
    )


class TriageResponse(BaseModel):
    """Unified response containing the incident report, alert payload and metrics."""

    incident_report: IncidentReport
    notification_payload: Dict[str, Any]
    metadata: Dict[str, Any]


@router.get("/api/v1/health", status_code=status.HTTP_200_OK)
async def health_check() -> Dict[str, str]:
    """Health check endpoint for monitoring API and service availability."""
    return {
        "status": "healthy",
        "service": "incident-triage-engine",
        "version": "1.0.0",
    }


@router.post(
    "/api/v1/triage",
    summary="Ingest raw logs and generate a structured triage report",
    response_model=TriageResponse,
    responses={
        200: {"description": "JSON TriageResponse, or an HTML fragment for HTMX requests"},
        502: {"description": "Analyzer output failed IncidentReport schema validation"},
    },
)
async def triage_logs(
    request: Request,
    payload: TriageRequest,
) -> Union[HTMLResponse, JSONResponse]:
    """
    Parse and compress the incoming log stream, retrieve matching runbooks, request a
    schema-validated IncidentReport from the analyzer, and return either JSON (API and
    webhooks) or an HTMX HTML fragment (dashboard requests).
    """
    if not payload.raw_logs.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="raw_logs string cannot be empty or whitespace only.",
        )

    try:
        results = engine.process_raw_logs(
            payload.raw_logs,
            max_context_lines=payload.max_context_lines,
        )
    except ValidationError as exc:
        # The analyzer returned something that is not an IncidentReport. Surface it as a
        # provider failure instead of silently substituting a heuristic report.
        logger.warning("Analyzer output failed schema validation: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Analyzer output failed schema validation: {exc.errors()}",
        ) from exc
    except Exception as exc:
        logger.exception("Triage execution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during log triage execution: {exc}",
        ) from exc

    metadata = results["metadata"]
    logger.debug(
        "Triage %s: %d logs ingested, %d error logs, %d unparseable, degraded=%s",
        results["incident_report"].get("incident_id"),
        metadata.get("total_logs_ingested"),
        metadata.get("error_logs_analyzed"),
        metadata.get("unparseable_lines"),
        metadata.get("degraded"),
    )

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(content=render_triage_html(results))

    return JSONResponse(content=results)


def _esc(value: Any) -> str:
    """Escape report strings before they are interpolated into HTML."""
    return html.escape(str(value), quote=True)


def render_triage_html(report: Union[Dict[str, Any], IncidentReport]) -> str:
    # 1. Unwrap nested incident_report dictionary if present
    if isinstance(report, dict) and "incident_report" in report:
        report = report["incident_report"]

    # 2. Helper to safely look up values across dicts and Pydantic objects
    def get_val(obj: Any, key: str, default: Any = None) -> Any:
        if obj is None:
            return default
        if isinstance(obj, dict):
            val = obj.get(key)
        else:
            val = getattr(obj, key, None)
        return val if val is not None else default

    incident_id = _esc(get_val(report, "incident_id", "INC-Default-ID"))
    title = _esc(get_val(report, "title", "Title"))

    # Extract enum string value if severity is an Enum
    raw_sev = get_val(report, "severity", "LOW")
    severity = _esc(str(getattr(raw_sev, "value", raw_sev)).upper())

    summary = _esc(get_val(report, "summary", "No summary provided."))
    affected_services: List[str] = get_val(report, "affected_services", [])
    anomalies: List[Any] = get_val(report, "anomalies", [])
    actions: List[str] = get_val(report, "recommended_actions", [])
    requires_escalation = get_val(report, "requires_escalation", True)

    severity_colors = {
        "CRITICAL": "text-rose-400 border-rose-500/30 bg-rose-500/10",
        "HIGH": "text-amber-400 border-amber-500/30 bg-amber-500/10",
        "MEDIUM": "text-yellow-400 border-yellow-500/30 bg-yellow-500/10",
        "LOW": "text-emerald-400 border-emerald-500/30 bg-emerald-500/10",
        "INFO": "text-slate-400 border-slate-700 bg-slate-900",
    }
    sev_class = severity_colors.get(severity, "text-slate-400 border-slate-700 bg-slate-900")

    services_badges = "".join(
        f'<span class="inline-block px-2 py-0.5 text-xs font-mono rounded bg-slate-700 '
        f'text-slate-300 mr-1 mb-1">{_esc(svc)}</span>'
        for svc in affected_services
    ) or '<span class="text-xs text-slate-500">None</span>'

    actions_html = (
        "".join(f"<li class='mt-1'>{_esc(act)}</li>" for act in actions)
        or "<li>No immediate actions listed.</li>"
    )

    # Render detected anomalies
    anomalies_list = []
    for a in anomalies:
        if isinstance(a, dict):
            svc = a.get("service_name", "unknown")
            code = a.get("error_code", "")
            msg = a.get("message", a.get("raw_log_line", ""))
            ts = a.get("timestamp", "")
        else:
            svc = getattr(a, "service_name", "unknown")
            code = getattr(a, "error_code", "")
            msg = getattr(a, "message", getattr(a, "raw_log_line", ""))
            ts = getattr(a, "timestamp", "")

        anomalies_list.append(
            f"""
            <div class="p-2 rounded bg-slate-900/60 border border-slate-700/50 space-y-1">
                <div class="flex items-center justify-between text-xs font-mono text-slate-400">
                    <span class="text-rose-400 font-semibold">[{_esc(svc)}]{f' Error {_esc(code)}' if code else ''}</span>
                    <span class="text-[10px] text-slate-500">{_esc(ts)}</span>
                </div>
                <p class="text-xs text-slate-300 font-mono break-all">{_esc(msg)}</p>
            </div>
            """
        )

    anomalies_html = (
        "".join(anomalies_list)
        or '<p class="text-xs text-slate-500 italic">No specific anomalies flagged.</p>'
    )

    escalation_badge = (
        '<span class="px-2 py-0.5 text-xs font-medium rounded bg-rose-900/50 text-rose-300 '
        'border border-rose-700/50">Escalation Required</span>'
        if requires_escalation
        else '<span class="px-2 py-0.5 text-xs font-medium rounded bg-slate-700 text-slate-400">'
        'Standard Handling</span>'
    )

    degraded_note = ""
    degraded_reason = get_val(report, "degraded_reason")
    if degraded_reason:
        degraded_note = (
            '<p class="text-xs text-amber-400 mt-2">Degraded analysis: '
            f'{_esc(degraded_reason)}. Treat the actions above as heuristic, not LLM-derived.</p>'
        )

    return f"""
    <div class="p-6 bg-slate-800 rounded-xl border border-slate-700 space-y-4">
        <div class="flex items-center justify-between border-b border-slate-700 pb-3">
            <div class="flex items-center space-x-3">
                <span class="px-3 py-1 text-xs font-semibold rounded-full border {sev_class}">
                    {severity}
                </span>
                <h3 class="text-base font-semibold text-slate-100">{title}</h3>
            </div>
            <span class="text-xs font-mono text-slate-400">{incident_id}</span>
        </div>

        <p class="text-sm text-slate-200">{summary}</p>
        {degraded_note}

        <div class="grid grid-cols-2 gap-4 text-xs py-2 border-y border-slate-700/50">
            <div>
                <span class="text-slate-400 block mb-1">Affected Services:</span>
                <div>{services_badges}</div>
            </div>
            <div>
                <span class="text-slate-400 block mb-1">Status:</span>
                <div>{escalation_badge}</div>
            </div>
        </div>

        <div class="space-y-2">
            <h4 class="text-xs font-semibold text-slate-300 uppercase tracking-wider">Detected Anomalies:</h4>
            <div class="space-y-2">
                {anomalies_html}
            </div>
        </div>

        <div class="space-y-1 pt-2 border-t border-slate-700/50">
            <h4 class="text-xs font-semibold text-slate-300 uppercase tracking-wider">Recommended Actions:</h4>
            <ul class="text-xs text-slate-300 list-disc list-inside space-y-1">
                {actions_html}
            </ul>
        </div>
    </div>
    """
