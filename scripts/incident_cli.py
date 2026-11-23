"""Command-line triage: read a log file and print the structured incident report.

The CLI runs the same pipeline the API does (parse -> optional runbook retrieval ->
schema-validated report), so a report printed here is a report the service would
produce. Report storage is not wired in here; the API endpoints own persistence.
"""

import argparse
import json
import os
import sys
from typing import List, Optional

from pydantic import ValidationError

# Add the parent directory to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import settings  # noqa: E402
from src.engine import IncidentTriageEngine  # noqa: E402
from src.llm_client import StructuredLogAnalyzer  # noqa: E402


def read_log_file(log_file_path: str) -> List[str]:
    """Return the non-empty lines of a log file, in order."""
    with open(log_file_path, "r", encoding="utf-8") as log_file:
        return [line for line in log_file.read().splitlines() if line.strip()]


def build_engine(
    mode: str,
    model: Optional[str] = None,
    use_runbooks: bool = False,
    max_context_lines: int = 50,
) -> IncidentTriageEngine:
    """Engine wired to the requested backend, so the CLI can be run offline."""
    analyzer = StructuredLogAnalyzer(mode=mode, model=model)
    return IncidentTriageEngine(
        analyzer=analyzer,
        use_runbooks=use_runbooks,
        max_context_lines=max_context_lines,
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process support logs and generate a structured incident report."
    )
    parser.add_argument("log_file_path", type=str, help="Path to the log file.")
    parser.add_argument(
        "--mode",
        default=settings.TRIAGE_MODE,
        choices=("mock", "ollama", "openai"),
        help="Analysis backend; defaults to TRIAGE_MODE from the environment.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name for ollama/openai; env default when omitted.",
    )
    parser.add_argument(
        "--runbooks",
        action="store_true",
        help="Retrieve matching docs/runbooks/*.md before analysis. Opt-in: it loads "
        "an embedding model on first use.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum lines kept in the compressed context window.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Run triage and return a process exit code."""
    args = parse_args(argv)

    try:
        lines = read_log_file(args.log_file_path)
    except OSError as exc:
        print(f"Could not read log file '{args.log_file_path}': {exc}", file=sys.stderr)
        return 1

    if not lines:
        print(
            f"No log lines found in '{args.log_file_path}'.",
            file=sys.stderr,
        )
        return 1

    try:
        engine = build_engine(
            mode=args.mode,
            model=args.model,
            use_runbooks=args.runbooks,
            max_context_lines=args.limit,
        )
    except ValueError as exc:
        # e.g. openai mode without OPENAI_API_KEY.
        print(f"Cannot start analysis backend: {exc}", file=sys.stderr)
        return 1

    try:
        result = engine.process_raw_logs("\n".join(lines), max_context_lines=args.limit)
    except ValidationError as exc:
        # Mirrors the API's 502: bad analyzer output is reported, not substituted.
        print(
            f"Analyzer output failed schema validation: {exc.errors()}",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(result["incident_report"], indent=2))
    print(json.dumps(result["metadata"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
