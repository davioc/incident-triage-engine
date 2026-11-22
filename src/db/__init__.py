"""Database-backed storage for analysed triage reports.

The repository layer is dialect-agnostic: production runs on Postgres and the test
suite runs the same code against SQLite in memory (see
docs/adr/0002-database-selection.md).
"""

from src.db.models import Base, TriageReportRow
from src.db.repository import (
    delete_report,
    get_report,
    list_reports,
    save_report,
    storage_state,
)
from src.db.session import get_engine, reset_engine, session_scope, storage_enabled

__all__ = [
    "Base",
    "TriageReportRow",
    "delete_report",
    "get_engine",
    "get_report",
    "list_reports",
    "reset_engine",
    "save_report",
    "session_scope",
    "storage_enabled",
    "storage_state",
]
