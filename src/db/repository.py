"""Repository layer for stored triage reports.

Every function takes an optional ``url`` so callers and tests can aim the same code
at a different database without mutating global configuration.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import make_url, select

from src.config import settings
from src.db.models import TriageReportRow
from src.db.session import public_url, session_scope, storage_enabled


def session_url(url: Optional[str] = None) -> str:
    return (url or settings.DATABASE_URL).strip()


def storage_state(url: Optional[str] = None) -> Dict[str, Any]:
    """What storage is configured, with credentials removed from the URL."""
    if not storage_enabled():
        return {
            "enabled": False,
            "reason": "DATABASE_URL is not set; report storage is disabled",
        }
    return {
        "enabled": True,
        # ``postgresql+psycopg`` -> ``postgresql``: the driver suffix says which
        # driver is in use, the prefix names the backend an operator cares about.
        "backend": make_url(session_url(url)).drivername.split("+")[0],
        "url": public_url(url),
        "table": TriageReportRow.__tablename__,
    }


def save_report(
    result: Dict[str, Any],
    raw_log: Optional[str] = None,
    url: Optional[str] = None,
) -> str:
    """Store a triage result and return the new report id.

    ``result`` is the engine's own output (``incident_report`` + ``metadata``), so
    storage keeps the provenance the analysis produced rather than re-deriving it.
    """
    report = result.get("incident_report")
    if not report:
        raise ValueError("triage result has no incident_report to store")
    meta = result.get("metadata") or {}

    # Enum members are stored as their value so the column and the JSON payload read
    # the same way back out of either dialect.
    severity = getattr(report["severity"], "value", report["severity"])
    stored_report = {**report, "severity": severity}

    row = TriageReportRow(
        incident_id=report["incident_id"],
        title=report["title"],
        severity=severity,
        summary=report["summary"],
        requires_escalation=bool(report.get("requires_escalation", False)),
        analysis_mode=meta.get("analysis_mode"),
        model=meta.get("model"),
        degraded_reason=meta.get("degraded"),
        raw_log=raw_log,
        report=stored_report,
        report_metadata=meta,
        unparseable_lines=int(meta.get("unparseable_lines", 0)),
    )

    with session_scope(url) as session:
        session.add(row)
        session.flush()
        return row.id


def get_report(report_id: str, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Full stored report by id, or None when the id is unknown."""
    with session_scope(url) as session:
        row = session.get(TriageReportRow, report_id)
        return row.to_dict() if row else None


def list_reports(
    limit: int = 50,
    severity: Optional[str] = None,
    url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Newest first, in short form so a listing does not ship every payload."""
    statement = (
        select(TriageReportRow).order_by(TriageReportRow.created_at.desc()).limit(limit)
    )
    if severity:
        statement = statement.where(TriageReportRow.severity == severity)

    with session_scope(url) as session:
        return [row.summary_dict() for row in session.scalars(statement).all()]


def delete_report(report_id: str, url: Optional[str] = None) -> bool:
    """Delete by id; returns False when the id was not stored."""
    with session_scope(url) as session:
        row = session.get(TriageReportRow, report_id)
        if row is None:
            return False
        session.delete(row)
        return True
