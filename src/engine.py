"""
Incident Triage Engine orchestrating log parsing, runbook retrieval, structured LLM
analysis, and alert payload formatting.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from src.log_parser import parse_log_line
from src.llm_client import StructuredLogAnalyzer
from src.schema import IncidentReport, SeverityLevel

logger = logging.getLogger("incident-triage-engine.engine")


class IncidentTriageEngine:
    """End-to-end telemetry pipeline for log extraction, analysis, and alerting."""

    def __init__(
        self,
        analyzer: Optional[StructuredLogAnalyzer] = None,
        max_context_lines: int = 50,
        use_runbooks: bool = False,
        runbook_top_k: int = 2,
    ):
        """
        Initialize the engine with an optional pre-configured StructuredLogAnalyzer.
        If analyzer is None, lazy-initialization occurs on first run.

        use_runbooks enables retrieval of docs/runbooks/*.md from ChromaDB so the
        analyzer can ground its recommendations in the team's own remediation docs.
        Retrieval is opt-in because it loads an embedding model on first use.
        """
        self.analyzer = analyzer
        self.max_context_lines = max_context_lines
        self.use_runbooks = use_runbooks
        self.runbook_top_k = runbook_top_k

    def _get_analyzer(self) -> StructuredLogAnalyzer:
        """Lazy-initialize analyzer if not provided during init."""
        if self.analyzer is None:
            self.analyzer = StructuredLogAnalyzer()
        return self.analyzer

    def _retrieve_runbooks(self, query: str) -> Tuple[str, int, Optional[str]]:
        """
        Return (runbook_context, matched_count, warning). Retrieval failures are
        reported explicitly rather than silently degrading the analysis.
        """
        if not self.use_runbooks or not query.strip():
            return "", 0, None

        try:
            from src.vector_store import get_vector_store

            matches = get_vector_store().query_runbooks(
                query, n_results=self.runbook_top_k
            )
        except Exception as exc:
            logger.warning(
                "Runbook retrieval unavailable (%s); continuing without it.", exc
            )
            return "", 0, f"runbook retrieval unavailable: {exc}"

        if not matches:
            return "", 0, None

        blocks = [
            f"--- Runbook: {m['metadata'].get('title', 'untitled')} ---\n{m['content']}"
            for m in matches
        ]
        return "\n\n".join(blocks), len(matches), None

    def process_raw_logs(
        self, raw_logs: str, max_context_lines: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Full triage pipeline (ADR 0001 stages 1-4; stage 5 rendering is the API layer):
        1. Extract and filter high-signal log lines (compression).
        2. Retrieve matching runbooks and generate a structured IncidentReport.
        3. Format the Slack alert payload for the report.

        max_context_lines is passed per call so concurrent requests cannot mutate
        shared engine state.
        """
        limit = max_context_lines or self.max_context_lines

        # Stage 1: Filter and compress logs to isolate anomalies
        lines = [line for line in raw_logs.strip().splitlines() if line.strip()]
        parsed_entries = [
            entry for line in lines if (entry := parse_log_line(line)) is not None
        ]

        # Unparseable lines are counted instead of being dropped silently.
        unparseable = len(lines) - len(parsed_entries)
        if unparseable:
            logger.warning(
                "%d of %d log lines did not match the expected log format.",
                unparseable,
                len(lines),
            )

        # Extract errors based on level or HTTP status >= 400
        error_entries = [
            entry
            for entry in parsed_entries
            if entry.get("level") in ("ERROR", "CRITICAL", "FATAL")
            or entry.get("status", 0) >= 400
        ]

        # Compress context window
        if error_entries:
            lines_to_analyze = [
                (
                    f"{e['timestamp']} [{e['level']}] {e['status']} "
                    f"{e['latency_ms']}ms - {e['message']}"
                )
                for e in error_entries[:limit]
            ]
            compressed_context = "\n".join(lines_to_analyze)
            runbook_query = "\n".join(e["message"] for e in error_entries[:limit])
        else:
            compressed_context = "\n".join(lines[:limit])
            runbook_query = ""

        # Stage 2: Runbook retrieval, then structured analysis
        runbook_context, runbooks_matched, runbook_warning = self._retrieve_runbooks(
            runbook_query
        )

        analyzer = self._get_analyzer()
        report: IncidentReport = analyzer.analyze_logs(
            compressed_context, runbook_context=runbook_context
        )

        degraded_reason = getattr(analyzer, "degraded_reason", None)

        # Stage 3: Route and format triage response
        notification_payload = self.format_notification_payload(report)

        return {
            "incident_report": report.model_dump(),
            "notification_payload": notification_payload,
            "metadata": {
                "total_logs_ingested": len(parsed_entries),
                "error_logs_analyzed": len(error_entries),
                "unparseable_lines": unparseable,
                "requires_escalation": report.requires_escalation,
                "analysis_mode": getattr(analyzer, "mode", None),
                "model": getattr(analyzer, "model", None),
                "degraded": degraded_reason,
                "runbooks_matched": runbooks_matched,
                "runbook_warning": runbook_warning,
            },
        }

    def format_notification_payload(self, report: IncidentReport) -> Dict[str, Any]:
        """Format an IncidentReport as a Slack alert payload.

        Slack is the only channel implemented; PagerDuty routing is planned with the
        Week 8 webhook work and is not built here.
        """
        color_map = {
            SeverityLevel.CRITICAL: "#FF0000",
            SeverityLevel.HIGH: "#FFA500",
            SeverityLevel.MEDIUM: "#FFFF00",
            SeverityLevel.LOW: "#00FF00",
            SeverityLevel.INFO: "#808080",
        }

        actions_list = "\n".join(
            [f"• {action}" for action in report.recommended_actions]
        )

        return {
            "channel": (
                "#alerts-critical" if report.requires_escalation else "#alerts-info"
            ),
            "attachments": [
                {
                    "color": color_map.get(report.severity, "#808080"),
                    "title": f"[{report.severity.value}] {report.title}",
                    "text": report.summary,
                    "fields": [
                        {
                            "title": "Incident ID",
                            "value": report.incident_id,
                            "short": True,
                        },
                        {
                            "title": "Affected Services",
                            "value": ", ".join(report.affected_services) or "None",
                            "short": True,
                        },
                        {
                            "title": "Recommended Actions",
                            "value": actions_list or "No actions specified.",
                            "short": False,
                        },
                    ],
                    "escalation_triggered": report.requires_escalation,
                }
            ],
        }
