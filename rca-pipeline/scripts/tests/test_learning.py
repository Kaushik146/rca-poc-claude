"""Regression coverage for scripts/learning.py."""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import learning as L  # type: ignore


class RecordReadRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_record_preserves_all_fields(self):
        L.record("INC-1", "high_error_rate + recent_deploy", "code",
                 fix_outcome="worked", prior_incident_id="PM-42",
                 files_changed=["a.py", "b.py"], diff_sha256="abc123",
                 confidence="high", root=self.root)
        rec = L.read_record("INC-1", self.root)
        assert rec is not None
        self.assertEqual(rec["incident_id"], "INC-1")
        self.assertEqual(rec["signal_pattern"], "high_error_rate + recent_deploy")
        self.assertEqual(rec["fix_kind"], "code")
        self.assertEqual(rec["fix_outcome"], "worked")
        self.assertEqual(rec["prior_incident_id"], "PM-42")
        self.assertEqual(rec["files_changed"], ["a.py", "b.py"])
        self.assertEqual(rec["diff_sha256"], "abc123")
        self.assertEqual(rec["confidence"], "high")

    def test_record_overwrites_prior_same_incident(self):
        L.record("INC-2", "pattern_a", "code", fix_outcome="unknown", root=self.root)
        L.record("INC-2", "pattern_b", "config", fix_outcome="worked", root=self.root)
        rec = L.read_record("INC-2", self.root)
        assert rec is not None
        self.assertEqual(rec["signal_pattern"], "pattern_b")
        self.assertEqual(rec["fix_kind"], "config")
        self.assertEqual(rec["fix_outcome"], "worked")

    def test_read_missing_returns_none(self):
        self.assertIsNone(L.read_record("INC-MISSING", self.root))


class Validation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_fix_kind_raises(self):
        with self.assertRaises(ValueError):
            L.record("INC-3", "pattern", "bogus_fix_kind", root=self.root)
    def test_invalid_outcome_raises(self):
        with self.assertRaises(ValueError):
            L.record("INC-4", "pattern", "code", fix_outcome="SUCCESS", root=self.root)
    def test_invalid_confidence_raises(self):
        with self.assertRaises(ValueError):
            L.record("INC-5", "pattern", "code", confidence="extremely_high", root=self.root)
    def test_empty_signal_pattern_raises(self):
        with self.assertRaises(ValueError):
            L.record("INC-6", "", "code", root=self.root)
        with self.assertRaises(ValueError):
            L.record("INC-6", "   ", "code", root=self.root)


class UpdateOutcome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_update_changes_outcome_only(self):
        L.record("INC-U", "pattern_x", "code", fix_outcome="unknown",
                 prior_incident_id="PM-9", root=self.root)
        L.update_outcome("INC-U", "worked", root=self.root)
        rec = L.read_record("INC-U", self.root)
        assert rec is not None
        self.assertEqual(rec["fix_outcome"], "worked")
        self.assertEqual(rec["signal_pattern"], "pattern_x")
        self.assertEqual(rec["fix_kind"], "code")
        self.assertEqual(rec["prior_incident_id"], "PM-9")
        self.assertIn("updated_at", rec)

    def test_update_on_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            L.update_outcome("INC-NONE", "worked", root=self.root)

    def test_update_with_invalid_outcome_raises(self):
        L.record("INC-V", "pattern", "code", root=self.root)
        with self.assertRaises(ValueError):
            L.update_outcome("INC-V", "TOTALLY_FIXED", root=self.root)


class Query(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        L.record("INC-A", "high_error_rate + deploy", "code", fix_outcome="worked", root=self.root)
        L.record("INC-B", "high_error_rate + page", "config", fix_outcome="rolled_back", root=self.root)
        L.record("INC-C", "high_error_rate + deploy", "code", fix_outcome="unknown", root=self.root)
        L.record("INC-D", "memory_leak + slow_growth", "infra", fix_outcome="worked", root=self.root)
    def tearDown(self):
        self.tmp.cleanup()

    def test_query_by_signal_pattern_substring(self):
        self.assertEqual(len(L.query(signal_pattern="high_error_rate", root=self.root)), 3)
    def test_query_by_fix_kind(self):
        ids = {r["incident_id"] for r in L.query(fix_kind="code", root=self.root)}
        self.assertEqual(ids, {"INC-A", "INC-C"})
    def test_query_by_outcome(self):
        ids = {r["incident_id"] for r in L.query(fix_outcome="worked", root=self.root)}
        self.assertEqual(ids, {"INC-A", "INC-D"})
    def test_query_combines_filters(self):
        ids = {r["incident_id"] for r in L.query(signal_pattern="high_error_rate", fix_outcome="worked", root=self.root)}
        self.assertEqual(ids, {"INC-A"})
    def test_query_sorts_worked_outcomes_first(self):
        outcomes = [r["fix_outcome"] for r in L.query(signal_pattern="high_error_rate", root=self.root)]
        self.assertEqual(outcomes[0], "worked")
        self.assertEqual(outcomes[-1], "rolled_back")
    def test_query_substring_is_case_insensitive(self):
        self.assertEqual(len(L.query(signal_pattern="HIGH_ERROR_RATE", root=self.root)), 3)


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        L.record("INC-S1", "pattern_alpha", "code", fix_outcome="worked", root=self.root)
        L.record("INC-S2", "pattern_alpha", "code", fix_outcome="worked", root=self.root)
        L.record("INC-S3", "pattern_alpha", "code", fix_outcome="rolled_back", root=self.root)
        L.record("INC-S4", "pattern_beta", "config", fix_outcome="worked", root=self.root)
    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_total_count(self):
        self.assertEqual(L.summary(self.root)["total_records"], 4)
    def test_summary_by_fix_kind(self):
        self.assertEqual(L.summary(self.root)["by_fix_kind"], {"code": 3, "config": 1})
    def test_summary_by_fix_outcome(self):
        self.assertEqual(L.summary(self.root)["by_fix_outcome"], {"worked": 3, "rolled_back": 1})
    def test_summary_success_rate_per_pattern(self):
        s = L.summary(self.root)
        alpha = s["pattern_success_rate"]["pattern_alpha"]
        self.assertEqual(alpha["total"], 3)
        self.assertEqual(alpha["worked"], 2)
        self.assertAlmostEqual(alpha["rate"], 2/3, places=4)


class CorruptionRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_file_skipped_in_query(self):
        good = self.root / ".rca" / "learnings" / "INC-GOOD.json"
        bad = self.root / ".rca" / "learnings" / "INC-BAD.json"
        good.parent.mkdir(parents=True, exist_ok=True)
        good.write_text(json.dumps({
            "incident_id": "INC-GOOD", "signal_pattern": "x", "fix_kind": "code",
            "fix_outcome": "worked", "confidence": "high",
            "recorded_at": "2026-05-08T10:00:00Z",
        }))
        bad.write_text("{not json")
        ids = {r["incident_id"] for r in L.all_records(self.root)}
        self.assertEqual(ids, {"INC-GOOD"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
