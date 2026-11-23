"""Repository and migration tests, run against SQLite instead of Postgres.

The repository code is the same one production uses; only the dialect differs, which
is what docs/adr/0002-database-selection.md commits to. Tests do not need a database
service, so the suite stays offline.
"""

import os

import pytest
from sqlalchemy import inspect

from alembic import command
from alembic.config import Config
from src.config import settings
from src.db import Base, TriageReportRow, get_engine, reset_engine, session_scope
from src.db.repository import delete_report, get_report, list_reports, save_report

MEMORY_URL = "sqlite:///:memory:"

TRIAGE_RESULT = {
    "incident_report": {
        "incident_id": "INC-2026-042",
        "title": "Payments latency spike",
        "severity": "HIGH",
        "summary": "Latency rose after the connection pool was exhausted.",
        "requires_escalation": True,
        "affected_services": ["payments", "gateway"],
        "anomalies": [],
        "recommended_actions": ["Raise the connection pool limit"],
    },
    "metadata": {
        "analysis_mode": "ollama",
        "model": "llama3.2",
        "degraded": None,
        "unparseable_lines": 3,
    },
}


@pytest.fixture()
def memory_db():
    """A fresh in-memory database per test, on the repository's own engine."""
    original = settings.DATABASE_URL
    settings.DATABASE_URL = MEMORY_URL
    reset_engine()
    Base.metadata.create_all(get_engine())
    yield MEMORY_URL
    settings.DATABASE_URL = original
    reset_engine()


def test_saved_report_reads_back_with_provenance(memory_db):
    report_id = save_report(TRIAGE_RESULT, raw_log="ERROR 500 - payments timeout")

    stored = get_report(report_id)
    assert stored is not None
    assert stored["incident_id"] == "INC-2026-042"
    assert stored["severity"] == "HIGH"
    assert stored["report"]["severity"] == "HIGH"
    assert stored["analysis_mode"] == "ollama"
    assert stored["model"] == "llama3.2"
    assert stored["degraded"] is None
    assert stored["unparseable_lines"] == 3
    assert stored["raw_log"] == "ERROR 500 - payments timeout"
    assert stored["metadata"]["analysis_mode"] == "ollama"


def test_unknown_report_id_reads_as_none(memory_db):
    assert get_report("not-stored") is None


def test_save_rejects_a_result_without_a_report(memory_db):
    with pytest.raises(ValueError, match="no incident_report"):
        save_report({"metadata": {}})


def test_listing_is_newest_first_and_honours_limit(memory_db):
    first = save_report(
        {
            **TRIAGE_RESULT,
            "incident_report": {
                **TRIAGE_RESULT["incident_report"],
                "incident_id": "INC-1",
            },
        }
    )
    second = save_report(
        {
            **TRIAGE_RESULT,
            "incident_report": {
                **TRIAGE_RESULT["incident_report"],
                "incident_id": "INC-2",
            },
        }
    )

    listing = list_reports(limit=1)
    assert len(listing) == 1
    assert listing[0]["id"] == second  # newest first
    assert "report" not in listing[0]  # short form only

    assert {row["id"] for row in list_reports()} == {first, second}


def test_listing_filters_by_severity(memory_db):
    save_report(
        {
            **TRIAGE_RESULT,
            "incident_report": {
                **TRIAGE_RESULT["incident_report"],
                "severity": "CRITICAL",
                "incident_id": "INC-critical",
            },
        }
    )
    save_report(TRIAGE_RESULT)

    critical = list_reports(severity="CRITICAL")
    assert [row["incident_id"] for row in critical] == ["INC-critical"]


def test_delete_removes_only_the_named_report(memory_db):
    report_id = save_report(TRIAGE_RESULT)
    other = save_report(
        {
            **TRIAGE_RESULT,
            "incident_report": {
                **TRIAGE_RESULT["incident_report"],
                "incident_id": "INC-keep",
            },
        }
    )

    assert delete_report(report_id) is True
    assert delete_report(report_id) is False
    assert get_report(report_id) is None
    assert get_report(other) is not None


def test_session_scope_rolls_back_on_error(memory_db):
    with pytest.raises(RuntimeError):
        with session_scope() as session:
            session.add(
                TriageReportRow(
                    incident_id="INC-rollback",
                    title="Never stored",
                    severity="HIGH",
                    summary="The transaction rolled back.",
                    report={},
                    report_metadata={},
                )
            )
            raise RuntimeError("boom")

    assert list_reports() == []


def test_initial_migration_creates_the_reports_table(tmp_path):
    """The migration history, not just metadata.create_all, must produce the table."""
    db_file = tmp_path / "migrated.db"
    url = f"sqlite:///{db_file.as_posix()}"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = url
    try:
        command.upgrade(Config("alembic.ini"), "head")

        columns = {
            column["name"]
            for column in inspect(get_engine(url)).get_columns("triage_reports")
        }
        assert columns >= {
            "id",
            "incident_id",
            "title",
            "severity",
            "summary",
            "requires_escalation",
            "analysis_mode",
            "model",
            "degraded_reason",
            "raw_log",
            "report",
            "metadata",
            "unparseable_lines",
            "created_at",
        }

        command.downgrade(Config("alembic.ini"), "base")
        # Alembic keeps its own version table; only the application tables go away.
        assert set(inspect(get_engine(url)).get_table_names()) == {"alembic_version"}
    finally:
        settings.DATABASE_URL = original
        reset_engine()
        os.remove(db_file)
