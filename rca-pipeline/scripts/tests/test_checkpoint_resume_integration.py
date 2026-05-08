"""
End-to-end integration test for the orchestrator's checkpoint/resume contract.

Two layers of verification:

1. **CLI invocation contract** — simulates the sequence the orchestrator
   agent's prompt tells it to follow (read on start, write per phase,
   clear on success). Runs the sequence twice with a simulated mid-flight
   crash between phases. Asserts the second run reads the last good
   checkpoint, skips the phases that were already done, and resumes
   correctly.

2. **Prompt-contract prose lint** — verifies orchestrator.md still
   contains the non-negotiable resume tokens (CLI invocations, field
   names, behavioral instructions). Guards against silent drift in the
   markdown that would break resume without anything else catching it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
CHECKPOINT_SCRIPT = REPO_ROOT / "scripts" / "checkpoint.py"
ORCHESTRATOR_MD = REPO_ROOT / ".claude" / "agents" / "orchestrator.md"


def _cli(*args, root, stdin=None):
    cmd = [sys.executable, str(CHECKPOINT_SCRIPT), "--root", str(root), *args]
    res = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    return res.stdout, res.returncode


class CheckpointResumeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.incident = "INC-RESUME-1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_after_simulated_mid_flight_crash(self):
        out, rc = _cli("--read", "--incident-id", self.incident, root=self.root)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

        intake_output = {
            "ticket_id": self.incident, "title": "checkout slow",
            "raw_text_for_tws": "checkout failing intermittently this morning",
        }
        _, rc = _cli("--write", "--incident-id", self.incident,
                     "--phase", "intake", "--output", "@-",
                     "--rca-stage", "propose-only",
                     root=self.root, stdin=json.dumps(intake_output))
        self.assertEqual(rc, 0)

        signals_output = {
            "window": {"start": "08:25", "end": "08:50"},
            "vendor_anomalies": [{"service": "inv", "severity": "HIGH"}],
            "deploys": [{"repo": "org/inv", "sha": "7c4f9a2", "merged_at": "08:10Z"}],
        }
        _, rc = _cli("--write", "--incident-id", self.incident,
                     "--phase", "signals", "--output", "@-",
                     root=self.root, stdin=json.dumps(signals_output))
        self.assertEqual(rc, 0)

        # Simulated crash. Orchestrator dies. Restart on same incident_id.
        out, rc = _cli("--read", "--incident-id", self.incident, root=self.root)
        self.assertEqual(rc, 0)
        cp = json.loads(out)
        self.assertEqual(cp["last_completed_phase"], "signals")
        self.assertEqual(cp["phase_outputs"]["intake"], intake_output)
        self.assertEqual(cp["phase_outputs"]["signals"], signals_output)
        self.assertNotIn("prior_incident", cp["phase_outputs"])
        self.assertNotIn("fix_and_test", cp["phase_outputs"])
        self.assertEqual(cp["rca_stage"], "propose-only")

        prior_output = {"matches": [{"source": "confluence", "id": "PM-42",
                                      "score": 6.85}], "novelty_flag": False}
        _, rc = _cli("--write", "--incident-id", self.incident,
                     "--phase", "prior_incident", "--output", "@-",
                     root=self.root, stdin=json.dumps(prior_output))
        self.assertEqual(rc, 0)

        ft_output = {"fix_applied": True, "diff_sha256": "c32d40ae" + "f" * 56,
                     "files_changed": ["python-inventory-service/app.py"]}
        _, rc = _cli("--write", "--incident-id", self.incident,
                     "--phase", "fix_and_test", "--output", "@-",
                     root=self.root, stdin=json.dumps(ft_output))
        self.assertEqual(rc, 0)

        out, _ = _cli("--read", "--incident-id", self.incident, root=self.root)
        cp = json.loads(out)
        self.assertEqual(cp["last_completed_phase"], "fix_and_test")
        self.assertEqual(set(cp["phase_outputs"].keys()),
                         {"intake", "signals", "prior_incident", "fix_and_test"})

        _, rc = _cli("--clear", "--incident-id", self.incident, root=self.root)
        self.assertEqual(rc, 0)

        out, rc = _cli("--read", "--incident-id", self.incident, root=self.root)
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_resume_does_not_advance_last_phase_on_repeated_write(self):
        intake_v1 = {"ticket_id": "INC-RW", "version": 1}
        intake_v2 = {"ticket_id": "INC-RW", "version": 2, "extra_field": "added"}
        _cli("--write", "--incident-id", "INC-RW", "--phase", "intake",
             "--output", "@-", root=self.root, stdin=json.dumps(intake_v1))
        _cli("--write", "--incident-id", "INC-RW", "--phase", "intake",
             "--output", "@-", root=self.root, stdin=json.dumps(intake_v2))
        out, _ = _cli("--read", "--incident-id", "INC-RW", root=self.root)
        cp = json.loads(out)
        self.assertEqual(cp["last_completed_phase"], "intake")
        self.assertEqual(cp["phase_outputs"]["intake"], intake_v2)


class OrchestratorResumeContractProseLint(unittest.TestCase):
    REQUIRED_TOKENS = [
        "scripts/checkpoint.py --read",
        "scripts/checkpoint.py --write",
        "scripts/checkpoint.py --clear",
        "last_completed_phase",
        "phase_outputs",
        "Skip every phase",
        "resume from the next phase",
        "atomically",
    ]

    def test_required_resume_tokens_present(self):
        text = ORCHESTRATOR_MD.read_text()
        missing = [tok for tok in self.REQUIRED_TOKENS if tok not in text]
        self.assertEqual(missing, [],
                         f"orchestrator.md missing resume contract tokens: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
