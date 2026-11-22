"""CLI tests: the command-line entry point must run the real pipeline offline."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from scripts.incident_cli import main
from src.schema import IncidentReport

# The parser's expected log format, plus one line it cannot read.
LOG_LINES = [
    "2026-11-22 10:00:01 [ERROR] 500 5200ms - payments DB connection timeout",
    "2026-11-22 10:00:02 [ERROR] 502 4300ms - gateway upstream refused",
    "2026-11-22 10:00:03 [INFO] 200 12ms - health check ok",
    "this line is not a log entry",
]


class Capturing(list):
    def write(self, x):
        self.append(x)

    def flush(self):
        pass


def parse_two_json_blocks(text):
    """The CLI prints the report then the metadata, one JSON object each."""
    decoder = json.JSONDecoder()
    report, end = decoder.raw_decode(text.strip())
    metadata = decoder.raw_decode(text.strip()[end:].strip())[0]
    return report, metadata


class TestIncidentCLI(unittest.TestCase):
    def setUp(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr

    def tearDown(self):
        sys.stdout = self._stdout
        sys.stderr = self._stderr

    def run_cli(self, argv):
        out, err = Capturing(), Capturing()
        sys.stdout, sys.stderr = out, err
        try:
            code = main(argv)
        finally:
            sys.stdout, sys.stderr = self._stdout, self._stderr
        return code, "".join(out), "".join(err)

    def write_logs(self, lines=LOG_LINES):
        tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".log")
        tmp.write("\n".join(lines) + "\n")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_mock_backend_prints_a_validated_report(self):
        path = self.write_logs()
        code, out, err = self.run_cli([path, "--mode", "mock"])

        self.assertEqual(code, 0, err)
        report, metadata = parse_two_json_blocks(out)
        IncidentReport(**report)
        self.assertEqual(metadata["analysis_mode"], "mock")
        self.assertEqual(metadata["total_logs_ingested"], 3)
        self.assertEqual(metadata["error_logs_analyzed"], 2)
        self.assertEqual(metadata["unparseable_lines"], 1)

    def test_missing_log_file_exits_non_zero(self):
        code, _out, err = self.run_cli(["does/not/exist.log", "--mode", "mock"])

        self.assertEqual(code, 1)
        self.assertIn("Could not read log file", err)

    def test_empty_log_file_exits_non_zero(self):
        code, _out, err = self.run_cli([self.write_logs([""]), "--mode", "mock"])

        self.assertEqual(code, 1)
        self.assertIn("No log lines found", err)

    @patch("scripts.incident_cli.StructuredLogAnalyzer")
    def test_backend_that_cannot_be_builds_exits_non_zero(self, mock_analyzer):
        mock_analyzer.side_effect = ValueError(
            "OPENAI_API_KEY is required for openai mode"
        )
        code, _out, err = self.run_cli([self.write_logs(), "--mode", "openai"])

        self.assertEqual(code, 1)
        self.assertIn("Cannot start analysis backend", err)
        self.assertIn("OPENAI_API_KEY", err)

    @patch("scripts.incident_cli.build_engine")
    def test_runbooks_and_limit_are_passed_to_the_engine(self, build_engine):
        engine = build_engine.return_value
        engine.process_raw_logs.return_value = {
            "incident_report": {"incident_id": "INC-1"},
            "metadata": {},
        }
        code, _out, _err = self.run_cli(
            [self.write_logs(), "--mode", "mock", "--runbooks", "--limit", "12"]
        )

        self.assertEqual(code, 0)
        kwargs = build_engine.call_args.kwargs
        self.assertTrue(kwargs["use_runbooks"])
        self.assertEqual(kwargs["max_context_lines"], 12)

    @patch("scripts.incident_cli.build_engine")
    def test_invalid_analyzer_output_exits_with_the_validation_code(self, build_engine):
        # A real ValidationError, the same one the pipeline raises for bad output.
        with self.assertRaises(ValidationError) as caught:
            IncidentReport(**{"incident_id": "", "title": "", "severity": "CRITICAL"})
        build_engine.return_value.process_raw_logs.side_effect = caught.exception

        code, _out, err = self.run_cli([self.write_logs(), "--mode", "mock"])

        self.assertEqual(code, 2)
        self.assertIn("failed schema validation", err)


if __name__ == "__main__":
    unittest.main()
