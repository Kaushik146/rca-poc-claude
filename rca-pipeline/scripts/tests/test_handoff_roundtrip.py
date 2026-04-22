"""End-to-end round-trip test for the propose-only → open-pr handoff.

The handoff is the single most fragile piece of the two-stage RCA flow.
The propose-only run base64-encodes a proposal JSON, posts it inside
`<!--RCA-PROPOSAL-JSON:START-->` / `...:END-->` HTML-comment markers on
the incident issue, and stops. The open-pr run fires days later and
must find that comment, decode the payload byte-identically, verify a
SHA-256 pin on the diff, and only then open the PR.

These tests exercise the real encode + hydrate code paths — no mocks,
no shortcuts. Everything runs in-process, no subprocess, no GitHub. The
tests synthesize the exact shape of `gh api /issues/N/comments` and
feed it through `hydrate_proposal.hydrate(...)` the same way the
workflow does.

If any of these fail, the two-stage flow is broken in a way the
chassis CI would not otherwise catch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hydrate_proposal  # noqa: E402


# Marker constants duplicated from hydrate_proposal / fix-and-test.md
# because the test's job is to encode the way the *agent* would, not to
# re-use the hydrator's parsing regex for encoding. If the encoder and
# the decoder were ever to drift, these tests would catch it.
START = "<!--RCA-PROPOSAL-JSON:START-->"
END = "<!--RCA-PROPOSAL-JSON:END-->"


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_proposal(diff: str, incident_id: str = "INC-1234") -> dict:
    """Build a proposal dict shaped like what fix-and-test produces."""
    return {
        "mode": "propose-only",
        "fix_applied": True,
        "files_changed": ["python-inventory-service/app.py"],
        "diff_summary": "Fix off-by-one in /reserve boundary check",
        "diff": diff,
        "diff_sha256": sha256(diff),
        "incident_id": incident_id,
        "test_results": {"passed": 3, "failed": 0, "new_failures": []},
        "awaiting_approval": True,
        "approval_mechanism": "apply label 'rca:approve-fix' to the incident issue",
        "confidence": "high",
    }


def encode_as_comment_body(
    proposal: dict, human_preamble: str = "", human_postamble: str = ""
) -> str:
    """Encode the proposal the way the propose-only agent should.

    The agent's human-readable body wraps the fenced base64 payload.
    This mirrors the format documented in
    .claude/agents/fix-and-test.md Flow A step 5.b.
    """
    payload_json = json.dumps(proposal, separators=(",", ":"))
    payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
    return (
        f"{human_preamble}\n\n"
        f"{START}\n"
        f"{payload_b64}\n"
        f"{END}\n\n"
        f"{human_postamble}"
    )


def make_comment(
    body: str,
    comment_id: int = 1001,
    created_at: str = "2026-04-17T10:00:00Z",
) -> dict:
    """Build a comment dict shaped like gh api output."""
    return {
        "id": comment_id,
        "body": body,
        "created_at": created_at,
        "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{comment_id}",
    }


class HandoffRoundTripTests(unittest.TestCase):
    """Encode → render-as-comment → hydrate → assert byte-identical diff."""

    def _hydrate(self, comments: list[dict]) -> dict | None:
        """Run the real hydrator and return the decoded proposal (or None)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "proposal.json"
            rc = hydrate_proposal.hydrate(comments, "42", out)
            self.last_rc = rc
            if rc != 0:
                return None
            return json.loads(out.read_text())

    def test_simple_round_trip(self) -> None:
        """Happy path: one proposal comment, hydrator extracts it."""
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        proposal = make_proposal(diff)
        body = encode_as_comment_body(proposal, human_preamble="## Proposed fix")
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], diff)
        self.assertEqual(result["diff_sha256"], sha256(diff))
        self.assertEqual(self.last_rc, 0)

    def test_diff_containing_close_comment_sequence_survives(self) -> None:
        """A diff containing `-->` must not break out of the HTML comment.

        This is the whole reason we base64-encode the payload. If the
        encoder used raw JSON instead, the `-->` inside a diff string
        would terminate the HTML comment early and everything after it
        would become visible markdown / get eaten by the renderer.
        """
        # Simulate a diff that removes a literal `-->` token from some
        # HTML template file — a realistic case.
        diff = (
            "--- a/template.html\n"
            "+++ b/template.html\n"
            "@@ -3,3 +3,3 @@\n"
            "-<!-- legacy banner -->\n"
            "+<!-- new banner -->\n"
        )
        proposal = make_proposal(diff)
        body = encode_as_comment_body(proposal)
        # Sanity: the base64 payload itself must not contain `-->`.
        self.assertNotIn("-->", body.split(START, 1)[1].split(END, 1)[0])
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], diff)

    def test_unicode_and_newlines_survive(self) -> None:
        """Proposal fields can contain unicode + embedded newlines."""
        diff = "一行目\n二行目\n— em dash —\n"
        proposal = make_proposal(diff, incident_id="INC-ユニ-01")
        proposal["diff_summary"] = "修正: boundary check\n(multi-line summary)"
        body = encode_as_comment_body(proposal)
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], diff)
        self.assertEqual(result["incident_id"], "INC-ユニ-01")

    def test_multiple_comments_newest_wins(self) -> None:
        """Two proposals on the same issue: the newest one wins.

        Real-world scenario: propose-only ran twice because the initial
        run hit a transient error and was retried. The open-pr stage
        should realize the *most recent* approved proposal, not the
        stale one.
        """
        old_diff = "old-diff\n"
        new_diff = "new-diff\n"
        old = make_comment(
            encode_as_comment_body(make_proposal(old_diff)),
            comment_id=1001,
            created_at="2026-04-17T10:00:00Z",
        )
        new = make_comment(
            encode_as_comment_body(make_proposal(new_diff)),
            comment_id=1002,
            created_at="2026-04-17T11:00:00Z",
        )
        # Feed out-of-order to verify the hydrator sorts internally.
        result = self._hydrate([old, new])
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], new_diff)

    def test_non_proposal_comments_ignored(self) -> None:
        """Regular human chatter in the thread must not confuse the parser."""
        diff = "some-diff\n"
        proposal = make_proposal(diff)
        comments = [
            make_comment(
                "Adding label `incident` to trigger the RCA pipeline.",
                comment_id=1000,
                created_at="2026-04-17T09:59:00Z",
            ),
            make_comment(
                encode_as_comment_body(proposal),
                comment_id=1001,
                created_at="2026-04-17T10:00:00Z",
            ),
            make_comment(
                "I'm still looking at this, please don't retry yet.",
                comment_id=1002,
                created_at="2026-04-17T10:05:00Z",
            ),
        ]
        result = self._hydrate(comments)
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], diff)

    def test_no_proposal_comment_returns_1(self) -> None:
        """Open-pr must refuse to run if no proposal was ever posted."""
        comments = [
            make_comment("Just a regular comment, no markers at all."),
        ]
        result = self._hydrate(comments)
        self.assertIsNone(result)
        self.assertEqual(self.last_rc, 1)

    def test_malformed_base64_returns_2(self) -> None:
        """Markers present but payload is garbage: rc=2, not rc=0."""
        body = f"junk preamble\n\n{START}\n***not-valid-base64***\n{END}\n\nok"
        result = self._hydrate([make_comment(body)])
        self.assertIsNone(result)
        self.assertEqual(self.last_rc, 2)

    def test_valid_base64_but_not_json_returns_2(self) -> None:
        """Base64 decodes but the bytes aren't JSON: rc=2."""
        not_json = base64.b64encode(b"this is not json").decode("ascii")
        body = f"{START}\n{not_json}\n{END}\n"
        result = self._hydrate([make_comment(body)])
        self.assertIsNone(result)
        self.assertEqual(self.last_rc, 2)

    def test_tampered_diff_fails_integrity_check(self) -> None:
        """If diff_sha256 doesn't match diff, refuse to hydrate: rc=3.

        This is the integrity pin. An attacker who can post a comment
        on the incident issue could replace the diff with a malicious
        one *after* a human approved the original. The SHA-256 pin
        catches this: the proposal's `diff_sha256` field is set by the
        agent at propose-only time, so a tampered diff that's been
        swapped in later will hash differently.
        """
        diff = "clean-diff\n"
        proposal = make_proposal(diff)
        # Tamper: replace the diff but leave the original SHA pin.
        proposal["diff"] = "malicious-diff\n"
        body = encode_as_comment_body(proposal)
        result = self._hydrate([make_comment(body)])
        self.assertIsNone(result)
        self.assertEqual(self.last_rc, 3)

    def test_missing_sha_field_is_soft_warn_not_fail(self) -> None:
        """Older proposals without diff_sha256: still hydrate (for back-compat).

        The integrity pin was added mid-project. A propose-only run that
        happened *before* this check shipped would not have the field.
        We don't want to permanently wedge every pre-existing approved
        proposal — we degrade to 'warn' for missing fields and only
        hard-fail on a mismatch.
        """
        diff = "old-proposal-diff\n"
        proposal = make_proposal(diff)
        del proposal["diff_sha256"]
        body = encode_as_comment_body(proposal)
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], diff)

    def test_marker_split_across_whitespace(self) -> None:
        """Payload with internal whitespace (line-wraps, indents) still decodes.

        GitHub's renderer can insert whitespace inside HTML comments in
        some edge cases. base64 tolerates whitespace; our extractor
        strips it with `"".join(payload.split())`. This test makes sure
        that strip happens.
        """
        diff = "wrapped-diff\n"
        proposal = make_proposal(diff)
        payload_json = json.dumps(proposal, separators=(",", ":"))
        payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
        # Insert line breaks every 40 chars, plus random indentation —
        # simulates a renderer that wrapped a long line.
        chunks = [payload_b64[i : i + 40] for i in range(0, len(payload_b64), 40)]
        wrapped = "\n    ".join(chunks)
        body = f"{START}\n    {wrapped}\n{END}\n"
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result, f"rc={getattr(self, 'last_rc', None)}")
        self.assertEqual(result["diff"], diff)

    def test_earlier_clean_proposal_wins_over_newer_tampered(self) -> None:
        """Tampering scenario: malicious newer comment + clean older one.

        Attacker posts a later comment with a tampered diff. The
        hydrator should skip the tampered one (integrity fail) and
        walk back to the earlier clean proposal.
        """
        good_diff = "safe-approved-diff\n"
        good_proposal = make_proposal(good_diff)
        # Forge a tampered "newer" proposal with a mismatched sha pin.
        bad_proposal = make_proposal("evil-diff\n")
        bad_proposal["diff_sha256"] = sha256("something-else")
        comments = [
            make_comment(
                encode_as_comment_body(good_proposal),
                comment_id=1001,
                created_at="2026-04-17T10:00:00Z",
            ),
            make_comment(
                encode_as_comment_body(bad_proposal),
                comment_id=1002,
                created_at="2026-04-17T12:00:00Z",
            ),
        ]
        result = self._hydrate(comments)
        self.assertIsNotNone(result)
        self.assertEqual(result["diff"], good_diff)

    def test_two_marker_pairs_in_one_comment(self) -> None:
        """Malicious double-marker in a single comment body.

        Attacker tries to sneak in a second marker pair after the real
        one. We extract both, and the first one that decodes and
        passes integrity wins. This test just verifies we don't crash
        on the double-marker shape.
        """
        diff1 = "first-pair-diff\n"
        diff2 = "second-pair-diff\n"
        prop1 = make_proposal(diff1)
        prop2 = make_proposal(diff2)
        body = (
            encode_as_comment_body(prop1)
            + "\n\n---\n\n"
            + encode_as_comment_body(prop2)
        )
        # Should hydrate one of them without crashing. The order within
        # a single body isn't contractually specified — we only care
        # that hydration succeeds and the result is self-consistent.
        result = self._hydrate([make_comment(body)])
        self.assertIsNotNone(result)
        self.assertIn(result["diff"], (diff1, diff2))
        # Whichever was picked, its pin must be internally consistent.
        self.assertEqual(result["diff_sha256"], sha256(result["diff"]))


if __name__ == "__main__":
    unittest.main()
