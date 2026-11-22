"""SQLAlchemy models for stored triage reports.

Types are deliberately generic (String/Integer/JSON/DateTime) so one model serves
both Postgres and the SQLite test double. Queryable facts live in columns; the
validated report and the pipeline metadata are stored as JSON documents next to
them, so nothing the model produced is reshaped by storage.
"""

from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, JSON, Integer, String, Text, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Naming constraints explicitly so Alembic generates stable identifiers instead of
# Postgres's hash-style names, which are unreadable in migration diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TriageReportRow(Base):
    """One triage run: the report, the logs it came from, and how it was produced."""

    __tablename__ = "triage_reports"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    incident_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200))
    severity: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(Text)
    requires_escalation: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )

    # How the report was produced, kept alongside the report so a heuristic fallback
    # is never mistaken for model output once it is read back from storage.
    analysis_mode: Mapped[Optional[str]] = mapped_column(String(16), default=None)
    model: Mapped[Optional[str]] = mapped_column(String(128), default=None)
    degraded_reason: Mapped[Optional[str]] = mapped_column(Text, default=None)

    raw_log: Mapped[Optional[str]] = mapped_column(Text, default=None)
    report: Mapped[Dict[str, Any]] = mapped_column(JSON)
    # ``metadata`` is reserved by the declarative base, so the attribute is named
    # differently and still maps to the ``metadata`` column.
    report_metadata: Mapped[Dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict
    )

    # Lines the parser could not read, kept as a column because it is the number an
    # operator wants to filter on when checking whether triage saw the whole burst.
    unparseable_lines: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        """Repository-facing view: identity, provenance and the stored report."""
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "title": self.title,
            "severity": self.severity,
            "summary": self.summary,
            "requires_escalation": self.requires_escalation,
            "analysis_mode": self.analysis_mode,
            "model": self.model,
            "degraded": self.degraded_reason,
            "unparseable_lines": self.unparseable_lines,
            "created_at": self.created_at,
            "report": self.report,
            "metadata": self.report_metadata,
            "raw_log": self.raw_log,
        }

    def summary_dict(self) -> Dict[str, Any]:
        """Short form for listings, so a list endpoint does not send every payload."""
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "title": self.title,
            "severity": self.severity,
            "requires_escalation": self.requires_escalation,
            "analysis_mode": self.analysis_mode,
            "model": self.model,
            "degraded": self.degraded_reason,
            "created_at": self.created_at,
        }
