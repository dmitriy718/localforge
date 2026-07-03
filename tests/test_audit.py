from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localforge.audit import list_run_summaries, read_audit_events, summarize_run


class AuditTests(unittest.TestCase):
    def test_read_audit_events_skips_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                "\n".join(
                    [
                        '{"event":"run_start","payload":{"run_id":"r1"},"ts":"2026-01-01T00:00:00Z"}',
                        "not-json",
                        '{"event":"bad","payload":[],"ts":"2026-01-01T00:00:01Z"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            events, invalid = read_audit_events(path)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "run_start")
        self.assertEqual(invalid, 2)

    def test_summarize_run_reports_complete_with_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260702-000000-test"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text(
                "\n".join(
                    [
                        '{"event":"run_start","payload":{"run_id":"r1"},"ts":"2026-01-01T00:00:00Z"}',
                        '{"event":"iteration_start","payload":{"iteration":1},"ts":"2026-01-01T00:00:01Z"}',
                        '{"event":"tool_call","payload":{"name":"shell"},"ts":"2026-01-01T00:00:02Z"}',
                        '{"event":"tool_result","payload":{"result":{"name":"shell","ok":false,"output":"failed"}},"ts":"2026-01-01T00:00:03Z"}',
                        '{"event":"protocol_error","payload":{"error":"bad output"},"ts":"2026-01-01T00:00:04Z"}',
                        '{"event":"run_final","payload":{"final":"Build completed with warnings."},"ts":"2026-01-01T00:00:05Z"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = summarize_run(run_dir)

        self.assertEqual(summary.run_id, "20260702-000000-test")
        self.assertEqual(summary.status, "complete-with-errors")
        self.assertEqual(summary.iterations, 1)
        self.assertEqual(summary.tool_calls, 1)
        self.assertEqual(summary.tool_failures, 1)
        self.assertEqual(summary.protocol_errors, 1)
        self.assertEqual(summary.final_preview, "Build completed with warnings.")

    def test_summarize_run_counts_mcp_error_output_as_tool_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "20260702-000000-mcp-error"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text(
                "\n".join(
                    [
                        '{"event":"run_start","payload":{},"ts":"2026-01-01T00:00:00Z"}',
                        '{"event":"tool_result","payload":{"result":{"name":"mcp_tool","ok":true,"output":"MCP error -32602: bad input"}},"ts":"2026-01-01T00:00:01Z"}',
                        '{"event":"run_final","payload":{"final":"done"},"ts":"2026-01-01T00:00:02Z"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = summarize_run(run_dir)

        self.assertEqual(summary.status, "complete-with-errors")
        self.assertEqual(summary.tool_failures, 1)

    def test_list_run_summaries_orders_newest_first_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            for name in ("20260702-000000-a", "20260702-000001-b"):
                run_dir = runs_dir / name
                run_dir.mkdir()
                (run_dir / "events.jsonl").write_text(
                    '{"event":"run_start","payload":{},"ts":"2026-01-01T00:00:00Z"}\n',
                    encoding="utf-8",
                )

            summaries = list_run_summaries(runs_dir, limit=1)

        self.assertEqual([summary.run_id for summary in summaries], ["20260702-000001-b"])


if __name__ == "__main__":
    unittest.main()
