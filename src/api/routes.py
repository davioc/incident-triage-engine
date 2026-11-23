"""
HTTP layer for the triage service: the canonical /api/v1 contract plus HTMX
fragment rendering for the dashboard. Both app objects (src/api/__init__.py for the
API, src/main.py for the dashboard) mount this router, so there is exactly one
implementation of each endpoint.
"""

import html
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.config import settings
from src.db import (
    delete_report,
    get_report,
    list_reports,
    save_report,
    storage_enabled,
    storage_state,
)
from src.engine import IncidentTriageEngine
from src.llm_client import StructuredLogAnalyzer
from src.schema import IncidentReport, SeverityLevel

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


class ReportSummary(BaseModel):
    """Listing entry: enough to identify a stored report without its payload."""

    id: str
    incident_id: str
    title: str
    severity: str
    requires_escalation: bool
    analysis_mode: Optional[str] = None
    model: Optional[str] = None
    degraded: Optional[str] = None
    created_at: datetime


class ReportListResponse(BaseModel):
    reports: List[ReportSummary]
    storage: Dict[str, Any]


class StoredReport(BaseModel):
    """A report read back from storage, with the provenance it was stored with."""

    id: str
    incident_id: str
    title: str
    severity: str
    summary: str
    requires_escalation: bool
    analysis_mode: Optional[str] = None
    model: Optional[str] = None
    degraded: Optional[str] = None
    unparseable_lines: int = 0
    created_at: datetime
    report: Dict[str, Any]
    metadata: Dict[str, Any]
    raw_log: Optional[str] = None


def persist_report(results: Dict[str, Any], raw_logs: str) -> Dict[str, Any]:
    """
    Store the triage result, and report what storage did.

    A storage failure never changes the analysis: the report is returned with the
    failure named in metadata.storage instead of the request turning into a 500.
    """
    if not storage_enabled():
        return {"enabled": False, "stored": False}

    try:
        report_id = save_report(results, raw_log=raw_logs)
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.warning("Report storage failed: %s", exc)
        return {"enabled": True, "stored": False, "error": str(exc)}

    return {"enabled": True, "stored": True, "report_id": report_id}


def require_storage() -> Dict[str, Any]:
    """
    Fail loudly when a reports endpoint is called with storage disabled. "Not
    configured" must not be mistaken for "no such report".
    """
    if not storage_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=storage_state()["reason"],
        )
    return storage_state()


def checked_query(value: Optional[str], message: str) -> None:
    """Reject a filter that cannot match anything instead of silently returning []."""
    if value is not None and value not in {s.value for s in SeverityLevel}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


# --- Runtime analysis-backend switching -------------------------------------------
# The dashboard offers these three choices; a curl/CI caller may pass any model name
# Ollama has pulled. Env vars still set the *default*, but a switch lives only in this
# process, so testing a model never requires editing or restarting with a new .env.
ANALYSIS_MODES = ("mock", "ollama", "openai")

MENU_LABELS = {
    "ollama": "llama 3.2",
    "openai": "Other OpenAI",
    "mock": "Mock mode (default)",
}

# Short, because this runs on every switch and a dead endpoint should not stall the menu.
MODEL_PROBE_TIMEOUT = 3.0


def ollama_model_warning(analyzer: Any) -> Optional[str]:
    """
    Ask the Ollama endpoint which models it has, so a switch to a model that is not there
    says so on the button instead of producing a degraded report later. A missing model is
    a configuration mistake ('this Ollama does not have it'), which reads very differently
    from an outage; the two look identical in a degraded summary otherwise.
    """
    if getattr(analyzer, "mode", None) != "ollama":
        return None
    model = getattr(analyzer, "model", None)
    if not model:
        return None

    try:
        available = [
            m.id for m in analyzer.client.models.list(timeout=MODEL_PROBE_TIMEOUT)
        ]
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any transport failure is a warning, not a 500
        return (
            f"Could not read the model list from {getattr(analyzer, 'base_url', None)} "
            f"({type(exc).__name__}: {exc}). The switch is still in effect."
        )

    # Ollama resolves a bare name to '<name>:latest', so either spelling is usable.
    if model in available or f"{model}:latest" in available:
        return None

    listed = ", ".join(sorted(available)) if available else "no models pulled on it"
    return (
        f"Model '{model}' is not on {getattr(analyzer, 'base_url', None)}. "
        f"It has: {listed}. Run 'ollama pull {model}' on that host, or point "
        f"OLLAMA_BASE_URL at an Ollama that has it."
    )


def mode_options() -> List[Dict[str, Optional[str]]]:
    """Menu entries, resolved from the environment at request time."""
    return [
        {
            "mode": "ollama",
            "label": MENU_LABELS["ollama"],
            "model": os.getenv("OLLAMA_MODEL", "llama3.2"),
        },
        {"mode": "openai", "label": MENU_LABELS["openai"], "model": None},
        {"mode": "mock", "label": MENU_LABELS["mock"], "model": None},
    ]


def current_mode_state() -> Dict[str, Any]:
    """
    Describe the analyzer the shared engine would use right now. A mode that cannot be
    built (openai without a key) is reported with a warning instead of raising, so the
    menu still renders and names the reason.
    """
    requested = os.getenv("TRIAGE_MODE", "mock").lower()
    try:
        analyzer = engine._get_analyzer()
    except ValueError as exc:
        return {
            "mode": requested,
            "model": None,
            "base_url": None,
            "api_key_present": bool(os.getenv("OPENAI_API_KEY")),
            "warning": str(exc),
        }
    return {
        "mode": getattr(analyzer, "mode", requested),
        "model": getattr(analyzer, "model", None),
        "base_url": getattr(analyzer, "base_url", None),
        "api_key_present": bool(os.getenv("OPENAI_API_KEY")),
        "warning": _model_warning_for(
            getattr(analyzer, "mode", requested), getattr(analyzer, "model", None)
        ),
    }


# The probe runs on a switch, not on every page load: a GET would pay the timeout on each
# dashboard render. The result is kept so the warning survives a reload until the next switch.
_warning_state: Dict[str, Any] = {"key": None, "text": None}


def _model_warning_for(mode: str, model: Optional[str]) -> Optional[str]:
    """The stored warning, but only while that same backend and model are still active."""
    if _warning_state["text"] and _warning_state["key"] == (mode, model):
        return _warning_state["text"]
    return None


def mode_label(state: Dict[str, Any]) -> str:
    """Short label shown on the Serving button, naming the model where one applies."""
    mode = state["mode"]
    model = state.get("model")
    if mode == "mock":
        return "Mock mode"
    return f"{model or MENU_LABELS.get(mode, mode)}"


@router.get(
    "/api/v1/mode",
    summary="Report the active analysis backend",
    # The Union[HTMLResponse, JSONResponse] annotation is for typing only; FastAPI must
    # not try to build a response model out of it.
    response_model=None,
    responses={200: {"description": "JSON mode state, or the HTMX menu fragment"}},
)
async def get_analysis_mode(request: Request) -> Union[HTMLResponse, JSONResponse]:
    """Read the active backend. HTMX requests get the menu fragment to render in place."""
    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(content=render_mode_html(current_mode_state()))
    return JSONResponse(content=current_mode_state())


@router.post(
    "/api/v1/mode",
    summary="Switch the analysis backend for this process",
    response_model=None,
    responses={
        200: {"description": "JSON mode state, or the HTMX menu fragment"},
        400: {"description": "Unknown mode, or a backend that cannot be built"},
    },
)
async def set_analysis_mode(
    request: Request,
    mode: str = Query(..., description="mock, ollama or openai"),
    model: Optional[str] = Query(
        None, description="Model name for ollama/openai; env default when omitted."
    ),
) -> Union[HTMLResponse, JSONResponse]:
    """
    Rebuild the analyzer and attach it to the shared engine. Nothing is written to
    .env: the switch lasts for the lifetime of the server process.
    """
    normalized = mode.lower()
    if normalized not in ANALYSIS_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown mode '{mode}'. Expected one of: {', '.join(ANALYSIS_MODES)}.",
        )

    try:
        analyzer = StructuredLogAnalyzer(mode=normalized, model=model)
    except ValueError as exc:
        # e.g. openai mode without OPENAI_API_KEY. Keep the previous analyzer running.
        logger.warning("Analysis mode switch rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    engine.analyzer = analyzer
    state = current_mode_state()
    # The switch succeeded either way; this only tells the operator whether the next
    # request will actually reach a model.
    warning = ollama_model_warning(analyzer)
    _warning_state["key"] = (state["mode"], state["model"])
    _warning_state["text"] = warning
    state["warning"] = warning
    logger.info("Analysis backend switched to %s (%s)", state["mode"], state["model"])
    if warning:
        logger.warning("Model check after switch: %s", warning)

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(content=render_mode_html(state))
    return JSONResponse(content=state)


@router.get("/api/v1/health", status_code=status.HTTP_200_OK)
async def health_check() -> Dict[str, Any]:
    """
    Health check endpoint for monitoring API and service availability. Reports the
    storage configuration too, so an operator can tell disabled storage apart from a
    database that is configured but unreachable.
    """
    return {
        "status": "healthy",
        "service": "incident-triage-engine",
        "version": settings.VERSION,
        "storage": storage_state(),
    }


@router.post(
    "/api/v1/triage",
    summary="Ingest raw logs and generate a structured triage report",
    response_model=TriageResponse,
    responses={
        200: {
            "description": "JSON TriageResponse, or an HTML fragment for HTMX requests"
        },
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
    metadata["storage"] = persist_report(results, payload.raw_logs)
    logger.debug(
        "Triage %s: %d logs ingested, %d error logs, %d unparseable, degraded=%s, "
        "stored=%s",
        results["incident_report"].get("incident_id"),
        metadata.get("total_logs_ingested"),
        metadata.get("error_logs_analyzed"),
        metadata.get("unparseable_lines"),
        metadata.get("degraded"),
        metadata["storage"].get("stored"),
    )

    if request.headers.get("HX-Request") == "true":
        return HTMLResponse(content=render_triage_html(results))

    return JSONResponse(content=results)


@router.get(
    "/api/v1/reports",
    summary="List stored triage reports, newest first",
    response_model=ReportListResponse,
    responses={
        400: {"description": "severity is not one of the IncidentReport levels"},
        503: {"description": "Report storage is disabled (DATABASE_URL unset)"},
    },
)
async def list_stored_reports(
    limit: int = Query(
        50, ge=1, le=200, description="Maximum reports to return in short form."
    ),
    severity: Optional[str] = Query(
        None, description="Filter by severity level, e.g. CRITICAL."
    ),
) -> Dict[str, Any]:
    """Short listing of stored reports; full payloads come from the report endpoint."""
    storage = require_storage()
    checked_query(
        severity,
        f"Unknown severity '{severity}'. Expected one of: "
        f"{', '.join(s.value for s in SeverityLevel)}.",
    )

    try:
        reports = list_reports(limit=limit, severity=severity)
    except SQLAlchemyError as exc:
        logger.warning("Report listing failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Report storage is unreachable: {exc}",
        ) from exc

    return {"reports": reports, "storage": storage}


@router.get(
    "/api/v1/reports/{report_id}",
    summary="Read one stored triage report",
    response_model=StoredReport,
    responses={
        404: {"description": "No report stored under this id"},
        503: {"description": "Report storage is disabled (DATABASE_URL unset)"},
    },
)
async def get_stored_report(report_id: str) -> Dict[str, Any]:
    """The stored report plus the mode, model and degraded reason it was produced with."""
    require_storage()

    try:
        stored = get_report(report_id)
    except SQLAlchemyError as exc:
        logger.warning("Report read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Report storage is unreachable: {exc}",
        ) from exc

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No triage report stored with id '{report_id}'.",
        )

    return stored


@router.delete(
    "/api/v1/reports/{report_id}",
    summary="Delete a stored triage report",
    responses={
        404: {"description": "No report stored under this id"},
        503: {"description": "Report storage is disabled (DATABASE_URL unset)"},
    },
)
async def delete_stored_report(report_id: str) -> Dict[str, Any]:
    """Remove a stored report. Deletion is idempotent only in the sense that a second
    delete reports 404 rather than pretending the row still existed."""
    require_storage()

    try:
        removed = delete_report(report_id)
    except SQLAlchemyError as exc:
        logger.warning("Report delete failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Report storage is unreachable: {exc}",
        ) from exc

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No triage report stored with id '{report_id}'.",
        )

    return {"deleted": report_id, "storage": storage_state()}


def _esc(value: Any) -> str:
    """Escape report strings before they are interpolated into HTML."""
    return html.escape(str(value), quote=True)


def render_mode_html(state: Dict[str, Any]) -> str:
    """
    The Serving control: a button naming the active backend, with a menu that opens
    upward from the bottom action row. Menu entries post to /api/v1/mode and swap this
    same fragment in place, so the label and the 'current' marker follow the switch.
    """
    current = state["mode"]
    rows: List[str] = []
    for opt in mode_options():
        hint = ""
        if opt["mode"] == "ollama":
            hint = f'<span class="ml-auto text-[10px] text-slate-500 font-mono">{_esc(opt["model"])}</span>'
        elif opt["mode"] == "openai" and not state["api_key_present"]:
            hint = '<span class="ml-auto text-[10px] text-rose-400">no API key</span>'

        row_class = (
            "flex items-center w-full text-left px-3 py-2 rounded-md text-xs "
            "font-medium transition-colors"
        )
        if opt["mode"] == current:
            rows.append(
                f'<div class="{row_class} bg-slate-800/70 text-sky-300">'
                f'<span class="w-2 h-2 rounded-full bg-sky-400 mr-2 shrink-0"></span>'
                f'{_esc(opt["label"])}{hint}</div>'
            )
        else:
            query = f"?mode={quote(opt['mode'])}"
            if opt["mode"] == "ollama" and opt["model"]:
                query += f"&model={quote(str(opt['model']))}"
            rows.append(
                f'<button type="button" hx-post="/api/v1/mode{query}" '
                f'hx-target="#mode-control" hx-swap="outerHTML" '
                f'class="{row_class} text-slate-300 hover:bg-slate-800 hover:text-slate-100">'
                f'{_esc(opt["label"])}{hint}</button>'
            )

    warning = ""
    if state.get("warning"):
        warning = (
            '<p class="text-[10px] text-rose-300 px-2 pt-1 break-all">'
            f"{_esc(state['warning'])}</p>"
        )

    detail = state.get("base_url") or (
        "local heuristics" if current == "mock" else "api.openai.com"
    )

    return f"""
    <div id="mode-control" class="relative shrink-0">
        <button type="button" onclick="toggleModeMenu()" aria-haspopup="true"
            class="px-4 py-2.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-sm transition-colors whitespace-nowrap flex items-center space-x-2">
            <span>Serving</span>
            <span class="text-sky-400 font-semibold">{_esc(mode_label(state))}</span>
            <svg class="w-3 h-3 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7"></path>
            </svg>
        </button>
        <div id="mode-menu" class="hidden absolute bottom-full left-0 mb-2 w-60 bg-slate-900 border border-slate-700 rounded-lg shadow-xl p-2 space-y-1 z-20">
            <p class="text-[10px] uppercase tracking-wider text-slate-500 px-2">Analysis backend</p>
            {''.join(rows)}
            {warning}
            <p class="text-[10px] text-slate-500 font-mono px-2 pt-1 border-t border-slate-800">{_esc(detail)}</p>
        </div>
    </div>
    """


def render_triage_html(report: Union[Dict[str, Any], IncidentReport]) -> str:
    # Metrics ride alongside the report in the results dict; keep them before unwrapping.
    meta = report.get("metadata", {}) if isinstance(report, dict) else {}

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
    sev_class = severity_colors.get(
        severity, "text-slate-400 border-slate-700 bg-slate-900"
    )

    services_badges = (
        "".join(
            f'<span class="inline-block px-2 py-0.5 text-xs font-mono rounded bg-slate-700 '
            f'text-slate-300 mr-1 mb-1">{_esc(svc)}</span>'
            for svc in affected_services
        )
        or '<span class="text-xs text-slate-500">None</span>'
    )

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

        anomalies_list.append(f"""
            <div class="p-2 rounded bg-slate-900/60 border border-slate-700/50 space-y-1">
                <div class="flex items-center justify-between text-xs font-mono text-slate-400">
                    <span class="text-rose-400 font-semibold">[{_esc(svc)}]{f' Error {_esc(code)}' if code else ''}</span>
                    <span class="text-[10px] text-slate-500">{_esc(ts)}</span>
                </div>
                <p class="text-xs text-slate-300 font-mono break-all">{_esc(msg)}</p>
            </div>
            """)

    anomalies_html = (
        "".join(anomalies_list)
        or '<p class="text-xs text-slate-500 italic">No specific anomalies flagged.</p>'
    )

    escalation_badge = (
        '<span class="px-2 py-0.5 text-xs font-medium rounded bg-rose-900/50 text-rose-300 '
        'border border-rose-700/50">Escalation Required</span>'
        if requires_escalation
        else '<span class="px-2 py-0.5 text-xs font-medium rounded bg-slate-700 text-slate-400">'
        "Standard Handling</span>"
    )

    degraded_note = ""
    # The reason lives in metadata.degraded; the report dict never carries it, so reading
    # only report["degraded_reason"] would leave a degraded analysis looking like a result.
    degraded_reason = get_val(report, "degraded_reason") or meta.get("degraded")
    if degraded_reason:
        degraded_note = (
            '<p class="text-xs text-amber-400 mt-2">Degraded analysis: '
            f"{_esc(degraded_reason)}. Treat the actions above as heuristic, not LLM-derived.</p>"
        )

    analysis_note = ""
    analysis_mode = meta.get("analysis_mode")
    if analysis_mode:
        model_suffix = f" · {meta.get('model')}" if meta.get("model") else ""
        analysis_note = (
            '<p class="text-[10px] text-slate-500 font-mono mt-1">'
            f"Analysis: {_esc(analysis_mode)}{_esc(model_suffix)}</p>"
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
        {analysis_note}

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
