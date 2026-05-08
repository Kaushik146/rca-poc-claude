"""Regression coverage for scripts/checkpoint.py."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))

import checkpoint as CP  # type: ignore


class CheckpointReadWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _path(self, incident_id):
        return self.root / ".rca" / "checkpoints" / f"{incident_id}.json"

    def test_read_missing_returns_none(self):
        self.assertIsNone(CP.read_checkpoint("INC-NONE", self.root))

    def test_write_then_read_roundtrip(self):
        out = {"window": {"start": "08:25", "end": "08:50"}, "confidence": 1.0}
        CP.write_checkpoint("INC-1", "intake", out, "propose-only", self.root)
        cp = CP.read_checkpoint("INC-1", self.root)
        assert cp is not None
        self.assertEqual(cp["incident_id"], "INC-1")
        self.assertEqual(cp["last_completed_phase"], "intake")
        self.assertEqual(cp["phase_outputs"]["intake"], out)
        self.assertEqual(cp["rca_stage"], "propose-only")
        self.assertEqual(cp["format_version"], CP.FORMAT_VERSION)

    def test_extends_phase_outputs(self):
        intake_out = {"ticket_id": "INC-2", "title": "checkout slow"}
        signals_out = {"window": {"start": "x", "end": "y"}, "deploys": []}
        CP.write_checkpoint("INC-2", "intake", intake_out, root=self.root)
        CP.write_checkpoint("INC-2", "signals", signals_out, root=self.root)
        cp = CP.read_checkpoint("INC-2", self.root)
        assert cp is not None
        self.assertEqual(cp["last_completed_phase"], "signals")
        self.assertEqual(cp["phase_outputs"]["intake"], intake_out)
        self.assertEqual(cp["phase_outputs"]["signals"], signals_out)

    def test_clear_removes_file(self):
        CP.write_checkpoint("INC-3", "intake", {"x": 1}, root=self.root)
        self.assertTrue(self._path("INC-3").exists())
        self.assertTrue(CP.clear_checkpoint("INC-3", self.root))
        self.assertFalse(self._path("INC-3").exists())
        self.assertIsNone(CP.read_checkpoint("INC-3", self.root))

    def test_clear_absent_is_noop(self):
        self.assertFalse(CP.clear_checkpoint("INC-NO", self.root))

    def test_unknown_phase_raises(self):
        with self.assertRaises(ValueError):
            CP.write_checkpoint("INC-4", "not-a-real-phase", {}, root=self.root)


class CheckpointAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_orphan_tmp_invisible_to_reader(self):
        v1 = {"phase": "intake", "marker": "v1"}
        CP.write_checkpoint("INC-A", "intake", v1, root=self.root)
        live = self.root / ".rca" / "checkpoints" / "INC-A.json"
        orphan = live.parent / "INC-A.partial.json.tmp"
        orphan.write_text(json.dumps({"format_version": 1, "incident_id": "INC-A",
                                       "last_completed_phase": "signals",
                                       "phase_outputs": {"signals": {"marker": "v2"}}}))
        cp = CP.read_checkpoint("INC-A", self.root)
        assert cp is not None
        self.assertEqual(cp["last_completed_phase"], "intake")
        self.assertEqual(cp["phase_outputs"]["intake"]["marker"], "v1")
        self.assertTrue(orphan.exists())


class CheckpointCorruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _live(self, incident_id):
        p = self.root / ".rca" / "checkpoints" / f"{incident_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_corrupt_json_returns_none(self):
        self._live("INC-X").write_text("{ not valid json")
        self.assertIsNone(CP.read_checkpoint("INC-X", self.root))

    def test_format_version_mismatch_refused(self):
        self._live("INC-Y").write_text(json.dumps({
            "format_version": 999, "incident_id": "INC-Y",
            "last_completed_phase": "signals", "phase_outputs": {"signals": {}},
        }))
        self.assertIsNone(CP.read_checkpoint("INC-Y", self.root))

    def test_wrong_incident_id_refused(self):
        self._live("INC-Z").write_text(json.dumps({
            "format_version": CP.FORMAT_VERSION, "incident_id": "INC-DIFFERENT",
            "last_completed_phase": "intake", "phase_outputs": {},
        }))
        self.assertIsNone(CP.read_checkpoint("INC-Z", self.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
