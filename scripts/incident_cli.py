import sys
import os
import argparse
import json
from pydantic import ValidationError

# Add the parent directory to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.schema import IncidentReport  # noqa: E402


def parse_logs(log_file_path):
    """Read the log file and return its non-empty lines (one JSON object per line)."""
    with open(log_file_path, "r", encoding="utf-8") as log_file:
        return [line for line in log_file.read().splitlines() if line.strip()]


def generate_incident_report(logs):
    anomalies = []
    for log in logs:
        try:
            anomaly = json.loads(log)
            anomalies.append(anomaly)
        except json.JSONDecodeError:
            raise ValueError(f"Malformed log entry: {log}")

    # Placeholder function to call an LLM and generate the incident report
    # This function should be replaced with actual LLM integration
    return {
        "incident_id": "INC-2026-001",
        "title": "Critical Database Connection Error",
        "severity": "CRITICAL",
        "summary": "The database connection is down, affecting all services.",
        "affected_services": ["ServiceA", "ServiceB"],
        "anomalies": anomalies,
        "recommended_actions": [
            "Check database server status",
            "Restart affected services",
        ],
        "requires_escalation": True,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Process support logs and generate an incident report."
    )
    parser.add_argument("log_file_path", type=str, help="Path to the log file.")
    args = parser.parse_args()

    logs = parse_logs(args.log_file_path)
    try:
        incident_report_data = generate_incident_report(logs)
        print(json.dumps(incident_report_data, indent=2))

        try:
            incident_report = IncidentReport(**incident_report_data)
            print(incident_report.model_dump_json(indent=2))
        except ValidationError as e:
            print("Validation Error:", e)
    except ValueError as e:
        print(e)


if __name__ == "__main__":
    main()
