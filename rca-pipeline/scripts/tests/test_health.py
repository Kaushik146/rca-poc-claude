"""Regression coverage for scripts/health.py."""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import health as H  # type: ignore


class RecordRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_record_then_read_preserves_fields(self):
        H.record_event("INC-1", "signals", "mcp_call_success", "info",
                       {"mcp": "datadog", "duration_ms": 1234}, self.root)
        events = H.read_events("INC-1", self.root)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["incident_id"], "INC-1")
        self.assertEqual(ev["phase"], "signals")
        self.assertEqual(ev["event_type"], "mcp_call_success")
        self.assertEqual(ev["severity"], "info")
        self.assertEqual(ev["details"], {"mcp": "datadog", "duration_ms": 1234})

    def test_record_is_append_only(self):
        for i in range(3):
            H.record_event("INC-2", "intake", f"event_{i}", "info", {"i": i}, self.root)
        events = H.read_events("INC-2", self.root)
        self.assertEqual(len(events), 3)
        for i, ev in enumerate(events):
            self.assertEqual(ev["event_type"], f"event_{i}")
            self.assertEqual(ev["details"]["i"], i)

    def test_read_returns_empty_for_unknown_incident(self):
        self.assertEqual(H.read_events("INC-MISSING", self.root), [])


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_phase_raises(self):
        with self.assertRaises(ValueError):
            H.record_event("INC-3", "not-a-real-phase", "x", "info", {}, self.root)
    def test_invalid_severity_raises(self):
        with self.assertRaises(ValueError):
            H.record_event("INC-4", "intake", "x", "FATAL", {}, self.root)
    def test_empty_event_type_raises(self):
        with self.assertRaises(ValueError):
            H.record_event("INC-5", "intake", "", "info", {}, self.root)


class CorruptionRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_line_in_middle_is_skipped(self):
        log = self.root / ".rca" / "health" / "INC-C.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        good1 = {"timestamp": "2026-05-08T10:00:00Z", "incident_id": "INC-C",
                 "phase": "intake", "event_type": "phase_started", "severity": "info", "details": {}}
        good2 = {"timestamp": "2026-05-08T10:05:00Z", "incident_id": "INC-C",
                 "phase": "signals", "event_type": "phase_started", "severity": "info", "details": {}}
        log.write_text(json.dumps(good1) + "\n{not valid json mid-write\n" + json.dumps(good2) + "\n")
        events = H.read_events("INC-C", self.root)
        self.assertEqual(len(events), 2)

    def test_blank_lines_skipped(self):
        log = self.root / ".rca" / "health" / "INC-B.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        good = {"timestamp": "2026-05-08T10:00:00Z", "incident_id": "INC-B",
                "phase": "intake", "event_type": "x", "severity": "info", "details": {}}
        log.write_text("\n\n" + json.dumps(good) + "\n\n")
        self.assertEqual(len(H.read_events("INC-B", self.root)), 1)


class SinceWindowParse(unittest.TestCase):
    def test_parse_since_seconds(self):
        self.assertEqual(H.parse_since("30s"), timedelta(seconds=30))
    def test_parse_since_minutes(self):
        self.assertEqual(H.parse_since("15m"), timedelta(minutes=15))
    def test_parse_since_hours(self):
        self.assertEqual(H.parse_since("24h"), timedelta(hours=24))
    def test_parse_since_days(self):
        self.assertEqual(H.parse_since("7d"), timedelta(days=7))
    def test_parse_since_invalid_raises(self):
        with self.assertRaises(ValueError): H.parse_since("forever")
        with self.assertRaises(ValueError): H.parse_since("24")
        with self.assertRaises(ValueError): H.parse_since("24h30m")


class CrossIncidentQuery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_recent_events_pulls_across_incidents(self):
        H.record_event("INC-X", "intake", "phase_started", "info", {}, self.root)
        H.record_event("INC-Y", "signals", "phase_started", "info", {}, self.root)
        events = H.all_recent_events(timedelta(hours=1), self.root)
        self.assertEqual({ev["incident_id"] for ev in events}, {"INC-X", "INC-Y"})

    def test_recent_events_filters_by_window(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = self.root / ".rca" / "health" / "INC-W.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            json.dumps({"timestamp": old_ts, "incident_id": "INC-W", "phase": "intake",
                        "event_type": "old", "severity": "info", "details": {}}) + "\n"
            + json.dumps({"timestamp": new_ts, "incident_id": "INC-W", "phase": "intake",
                          "event_type": "new", "severity": "info", "details": {}}) + "\n"
        )
        events = H.all_recent_events(timedelta(hours=24), self.root)
        types = {ev["event_type"] for ev in events}
        self.assertIn("new", types)
        self.assertNotIn("old", types)


class CheckCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_check_exits_zero_when_no_errors(self):
        H.record_event("INC-OK", "intake", "phase_started", "info", {}, self.root)
        H.record_event("INC-OK", "intake", "phase_completed", "info", {}, self.root)
        self.assertEqual(H.main(["--root", str(self.root), "check", "--since", "1h"]), 0)

    def test_check_exits_nonzero_on_error_event(self):
        H.record_event("INC-ERR", "signals", "mcp_auth_failure", "error",
                       {"mcp": "datadog"}, self.root)
        self.assertEqual(H.main(["--root", str(self.root), "check", "--since", "1h"]), 1)

    def test_check_scoped_to_incident_filters_by_incident(self):
        H.record_event("INC-ALPHA", "signals", "mcp_auth_failure", "error", {}, self.root)
        self.assertEqual(
            H.main(["--root", str(self.root), "check", "--since", "1h",
                    "--incident-id", "INC-BETA"]), 0,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
