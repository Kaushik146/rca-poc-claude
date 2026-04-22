"""Chassis-contract tests.

Each test here locks a non-obvious invariant that the rest of the CI
gates do NOT catch:

  1. time-window-selector surfaces uncertainty on no-evidence input
     (either `coverage_warning == "no_evidence_available"` OR top
     confidence < 0.4 must be true — otherwise the orchestrator's
     "refuse to guess" contract silently breaks).

  2. CRITICAL vendor anomalies outweigh LOW ones in the selector. If
     a future edit flattens the severity-scaled weights, Watchdog's
     severity field becomes decorative — the top-1 window will follow
     recency rather than impact.

  3. The propose-only comment payload for a realistic-sized diff fits
     comfortably inside GitHub's 65,536-character issue-comment body
     ceiling. This is the #1 shakedown risk flagged in
     docs/MCP_TOOL_AUDIT.md (GitHub MCP row). Breaks the two-stage
     handoff entirely if we ever blow past the ceiling.

  4. .claude/commands/rca.md retains every non-negotiable token
     (kill-switch env var, HITL env var, validator-between-phases,
     confidence threshold, never-merge). If someone refactors the
     command prose and drops a guardrail line, we catch it at PR
     time rather than at pilot time.

All tests run offline, no subprocess-to-claude, no GitHub calls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
SELECTOR_SCRIPT = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "time-window-selector"
    / "scripts"
    / "select_window.py"
)
RCA_COMMAND = REPO_ROOT / ".claude" / "commands" / "rca.md"


def run_selector(payload: dict, timeout: int = 60) -> dict:
    """Invoke the time-window-selector skill as a subprocess; return parsed JSON."""
    proc = subprocess.run(
        [sys.executable, str(SELECTOR_SCRIPT)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"selector exited {proc.returncode}\nstderr:\n"
            f"{proc.stderr.decode(errors='replace')}"
        )
    return json.loads(proc.stdout or b"{}")


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2: time-window-selector contracts
# ─────────────────────────────────────────────────────────────────────────────
class TimeWindowSelectorContractTests(unittest.TestCase):
    """Lock the selector's 'uncertainty' and 'severity weighting' contracts."""

    def test_no_evidence_surfaces_uncertainty(self):
        """With no metrics/deploys/pages/vendor anomalies, the selector must
        give the orchestrator a signal to bail. Either coverage_warning is
        set, or top confidence is below 0.4 (the documented escalation
        threshold in rca.md and the time-window-selector SKILL.md).

        Today it surfaces via `coverage_warning == "no_evidence_available"`.
        A future rewrite that removes the coverage warning MUST also lower
        the confidence — otherwise the orchestrator silently proceeds on a
        guessed window.
        """
        payload = {
            "ticket": {
                "title": "something's off",
                "description": "no details",
                "reported_at": "2026-04-18T10:00:00Z",
            },
            "lookback_hours": 12,
            "metrics": {},
            "deploys": [],
            "pages": [],
            "vendor_anomalies": [],
        }
        out = run_selector(payload)
        windows = out.get("windows") or []
        coverage = out.get("coverage_warning")

        has_low_conf = (not windows) or (windows[0]["confidence"] < 0.4)
        has_coverage_warn = coverage == "no_evidence_available"

        self.assertTrue(
            has_coverage_warn or has_low_conf,
            "Selector returned no uncertainty signal on an empty-evidence "
            f"payload. windows={windows!r} coverage={coverage!r}. "
            "Orchestrator will silently proceed on a guessed window.",
        )

    def test_critical_vendor_anomaly_outweighs_low(self):
        """CRITICAL vendor anomaly far from ticket-report time must still
        beat a LOW vendor anomaly near ticket-report time. Locks severity
        weighting (CRITICAL=1.3, LOW=0.4 in select_window.py).

        Regression target: if someone ever hoists recency over severity,
        this test fails immediately. Watchdog/Davis severity fields are
        the whole point of the 'honor the vendor' contract.
        """
        payload = {
            "ticket": {
                "title": "checkout errors",
                "description": "",
                "reported_at": "2026-04-18T10:00:00Z",
            },
            "lookback_hours": 12,
            "metrics": {},
            "deploys": [],
            "pages": [],
            "vendor_anomalies": [
                # CRITICAL — earlier, further from ticket time
                {
                    "service": "inventory-service",
                    "metric": "error_rate",
                    "severity": "CRITICAL",
                    "start": "2026-04-18T07:55:00Z",
                    "end": "2026-04-18T08:05:00Z",
                    "source": "datadog_watchdog",
                },
                # LOW — later, closer to ticket time
                {
                    "service": "inventory-service",
                    "metric": "error_rate",
                    "severity": "LOW",
                    "start": "2026-04-18T09:40:00Z",
                    "end": "2026-04-18T09:50:00Z",
                    "source": "datadog_watchdog",
                },
            ],
        }
        out = run_selector(payload)
        windows = out.get("windows") or []
        self.assertTrue(windows, "selector returned no windows")
        top_va = windows[0].get("supporting_evidence", {}).get("vendor_anomalies") or []
        top_severities = {v.get("severity") for v in top_va}
        self.assertIn(
            "CRITICAL",
            top_severities,
            f"Top window does not include the CRITICAL anomaly. "
            f"top severities={top_severities!r}, windows={windows!r}. "
            f"Severity weighting in select_window.py may have regressed.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3: propose-only comment body fits GitHub's issue-comment ceiling
# ─────────────────────────────────────────────────────────────────────────────
# GitHub's documented hard limit on issue-comment body length.
# Source: https://docs.github.com/en/rest/issues/comments (field: body, max 65536).
GITHUB_COMMENT_MAX_CHARS = 65_536

# Representative preamble/postamble text — mirrors what the propose-only
# agent prose produces above/below the fenced base64 payload.
HUMAN_PREAMBLE = (
    "## RCA proposal — INC-1234\n\n"
    "**Hypothesis:** Off-by-one in `/reserve` boundary check.\n\n"
    "**Test results:** 42 passed, 0 failed.\n\n"
    "**Files changed:** `python-inventory-service/app.py`\n\n"
    "To open this fix as a PR, apply the `rca:approve-fix` label to this "
    "issue. To cancel, apply `rca:reject`. No PR has been opened yet.\n\n"
    "<details><summary>Diff (read-only preview)</summary>\n\n"
    "```diff\n"
    "--- a/python-inventory-service/app.py\n"
    "+++ b/python-inventory-service/app.py\n"
    "```\n\n"
    "</details>\n"
)
HUMAN_POSTAMBLE = (
    "\n---\n"
    "Proposal payload is pinned via SHA-256; do not edit the HTML-comment "
    "block above. The open-pr stage refuses to run if the hash changes.\n"
)
MARKER_START = "<!--RCA-PROPOSAL-JSON:START-->"
MARKER_END = "<!--RCA-PROPOSAL-JSON:END-->"


def _encode_comment_body(proposal: dict) -> str:
    """Mirror the propose-only agent's comment format exactly."""
    payload_json = json.dumps(proposal, separators=(",", ":"))
    payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    return (
        f"{HUMAN_PREAMBLE}\n"
        f"{MARKER_START}\n"
        f"{payload_b64}\n"
        f"{MARKER_END}\n"
        f"{HUMAN_POSTAMBLE}"
    )


def _synth_diff(n_bytes: int) -> str:
    """Synthesize a unified-diff-looking string of approximately n_bytes."""
    line = (
        "+    if new_qty > item.stock:  # fixed boundary "
        "# was `>=`, off-by-one under concurrent /reserve calls\n"
    )
    body = line * (n_bytes // len(line) + 1)
    header = (
        "--- a/python-inventory-service/app.py\n"
        "+++ b/python-inventory-service/app.py\n"
        "@@ -142,7 +142,7 @@ def reserve():\n"
        "-    if new_qty >= item.stock:\n"
    )
    return (header + body)[:n_bytes]


class ProposalCommentSizeTest(unittest.TestCase):
    """Regression guard against the #1 shakedown risk: GitHub rejects any
    issue-comment body longer than 65,536 characters. If the propose-only
    agent ever starts embedding the full source file, or if the base64
    overhead + preamble creeps close to the limit, we want to catch it
    here rather than during a live incident."""

    def _build_proposal(self, diff: str) -> dict:
        return {
            "mode": "propose-only",
            "fix_applied": True,
            "files_changed": ["python-inventory-service/app.py"],
            "diff_summary": "Fix off-by-one in /reserve boundary check",
            "diff": diff,
            "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
            "incident_id": "INC-1234",
            "test_results": {"passed": 42, "failed": 0, "new_failures": []},
            "awaiting_approval": True,
            "approval_mechanism": "apply label 'rca:approve-fix' to the incident issue",
            "confidence": "high",
        }

    def test_typical_diff_fits_with_headroom(self):
        """A typical single-file RCA fix is <2 KB of diff. Comment body
        should be <10 KB — two orders of magnitude under the GH ceiling."""
        diff = _synth_diff(2_000)
        body = _encode_comment_body(self._build_proposal(diff))
        self.assertLess(
            len(body),
            10_000,
            f"Typical proposal comment grew unexpectedly large: "
            f"{len(body)} chars. Check the preamble format in fix-and-test.md.",
        )

    def test_large_diff_still_fits(self):
        """A 'large' RCA fix (20 KB of diff — e.g. multi-method refactor)
        must still fit with comfortable headroom under GH's 65,536 ceiling.
        20 KB raw → ~27 KB base64 → ~30 KB comment body, leaving ~35 KB
        buffer. If we ever drop below 10 KB of headroom, chunking becomes
        necessary and this test should be re-tuned (do not raise the
        ceiling — fix the encoding)."""
        diff = _synth_diff(20_000)
        body = _encode_comment_body(self._build_proposal(diff))
        self.assertLess(
            len(body),
            GITHUB_COMMENT_MAX_CHARS,
            f"20 KB diff produced a comment of {len(body)} chars, "
            f"exceeding GH limit {GITHUB_COMMENT_MAX_CHARS}.",
        )
        # Headroom sanity check — we want plenty of room for future growth
        # (markdown formatting, additional fields, etc.).
        headroom = GITHUB_COMMENT_MAX_CHARS - len(body)
        self.assertGreater(
            headroom,
            10_000,
            f"Large-diff comment has only {headroom} chars of headroom. "
            f"Consider chunking the payload before we run out of room.",
        )

    def test_ceiling_diff_documented(self):
        """Calibration: compute the largest raw diff that still fits end-to-
        end under the GH ceiling with the current preamble/postamble. This
        test doesn't fail — it prints the number so the shakedown playbook
        has a concrete value to reference ('any diff above N bytes needs
        chunking')."""
        # Binary search for the largest raw diff size that fits.
        lo, hi = 1_000, 60_000
        best_fit = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            body = _encode_comment_body(self._build_proposal(_synth_diff(mid)))
            if len(body) < GITHUB_COMMENT_MAX_CHARS:
                best_fit = mid
                lo = mid + 1
            else:
                hi = mid - 1
        # Print for the shakedown record. The test passes unconditionally —
        # its purpose is to surface the number, not to gate on it.
        print(
            f"\n  [INFO] Max raw-diff size that fits one proposal comment: "
            f"~{best_fit:,} bytes "
            f"(GH ceiling {GITHUB_COMMENT_MAX_CHARS:,}, pre/postamble "
            f"overhead {len(HUMAN_PREAMBLE) + len(HUMAN_POSTAMBLE) + len(MARKER_START) + len(MARKER_END):,}"
            f" chars + base64 1.33x)."
        )
        self.assertGreater(
            best_fit,
            30_000,
            f"Current encoder tops out at {best_fit} raw diff bytes — "
            f"a legitimate multi-file RCA patch likely won't fit.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4: rca.md non-negotiable prose retention
# ─────────────────────────────────────────────────────────────────────────────
# These tokens MUST appear verbatim in .claude/commands/rca.md. Each one
# corresponds to a production-contract guardrail. If any is removed, the
# slash command's behavior contract silently weakens — the agent running
# /rca has no other source of truth for these rules.
REQUIRED_RCA_TOKENS = [
    "RCA_DISABLED",              # kill switch env var
    "RCA_REQUIRE_FIX_APPROVAL",  # HITL env var
    "RCA_STAGE",                 # staged execution
    "propose-only",              # propose-only stage name
    "open-pr",                   # open-pr stage name
    "confidence < 0.4",          # selector escalation threshold
    "Never merge",               # no-auto-merge guardrail
    "validator",                 # validator-between-phases
    "rca:approve-fix",           # approval label
]


class RcaCommandProseLintTest(unittest.TestCase):
    """If any of these tokens disappear from rca.md, the slash command
    loses a production-contract guardrail. The test is exact-substring
    so it's deliberately brittle — that's the point."""

    def test_non_negotiable_tokens_present(self):
        self.assertTrue(RCA_COMMAND.is_file(), f"missing {RCA_COMMAND}")
        prose = RCA_COMMAND.read_text(encoding="utf-8")
        missing = [tok for tok in REQUIRED_RCA_TOKENS if tok not in prose]
        self.assertFalse(
            missing,
            f"rca.md is missing required guardrail token(s): {missing}. "
            f"Each token corresponds to a non-negotiable behavior contract. "
            f"Restore the prose or update this test with a documented reason.",
        )


if __name__ == "__main__":
    unittest.main()
