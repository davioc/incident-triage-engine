import unittest
from unittest.mock import patch, MagicMock
import sys
import json

from scripts.incident_cli import main


# Mock the print function to capture the output
class Capturing(list):
    def write(self, x):
        self.append(x)

    def flush(self):
        pass


class TestIncidentCLIScript(unittest.TestCase):

    @patch("argparse.ArgumentParser.parse_args")
    @patch("scripts.incident_cli.parse_logs")
    def test_correct_json_logs(self, mock_parse_logs, mock_parse_args):
        # Mock the log file content
        log_content = '{"timestamp": "2023-10-01T12:00:00Z", "service_name": "ServiceA", "error_code": "500", "message": "Database connection failed", "raw_log_line": "ERROR 2023-10-01T12:00:00Z ServiceA - Database connection failed"}'
        mock_parse_logs.return_value = [log_content]

        # Mock the command-line arguments
        mock_parse_args.return_value = MagicMock(log_file_path="path/to/log.json")

        # Redirect stdout to capture the output
        captured_output = Capturing()
        sys.stdout = captured_output

        # Call the main function
        main()

        # Restore stdout
        sys.stdout = sys.__stdout__

        # Check if the output contains the expected JSON
        expected_output = json.dumps(
            {
                "incident_id": "INC-2026-001",
                "title": "Critical Database Connection Error",
                "severity": "CRITICAL",
                "summary": "The database connection is down, affecting all services.",
                "affected_services": ["ServiceA", "ServiceB"],
                "anomalies": [
                    {
                        "timestamp": "2023-10-01T12:00:00Z",
                        "service_name": "ServiceA",
                        "error_code": "500",
                        "message": "Database connection failed",
                        "raw_log_line": "ERROR 2023-10-01T12:00:00Z ServiceA - Database connection failed",
                    }
                ],
                "recommended_actions": [
                    "Check database server status",
                    "Restart affected services",
                ],
                "requires_escalation": True,
            },
            indent=2,
        )
        self.assertIn(expected_output, "".join(captured_output))

    @patch("argparse.ArgumentParser.parse_args")
    @patch("scripts.incident_cli.parse_logs")
    def test_malformed_json_logs(self, mock_parse_logs, mock_parse_args):
        # Mock the log file content
        log_content = '{"timestamp": "2023-10-01T12:00:00Z", "service_name": "ServiceA", "error_code": "500", "message": "Database connection failed", "raw_log_line": "ERROR 2023-10-01T12:00:00Z ServiceA - Database connection failed'
        mock_parse_logs.return_value = [log_content]

        # Mock the command-line arguments
        mock_parse_args.return_value = MagicMock(log_file_path="path/to/log.json")

        # Redirect stdout to capture the output
        captured_output = Capturing()
        sys.stdout = captured_output

        # Call the main function
        main()

        # Restore stdout
        sys.stdout = sys.__stdout__

        # Check if the output contains the validation error
        self.assertIn("Malformed log entry", "".join(captured_output))


if __name__ == "__main__":
    unittest.main()
