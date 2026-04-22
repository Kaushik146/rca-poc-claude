"""Extract a propose-only proposal JSON from an incident issue's comments.

This is the cross-run handoff between the two RCA workflow stages:

  propose-only  →  posts a comment on the incident issue containing the
                   full proposal JSON, base64-encoded, fenced by
                   <!--RCA-PROPOSAL-JSON:START--> / <!--RCA-PROPOSAL-JSON:END-->
                   markers so GitHub's markdown renderer can't mangle it.

  open-pr       →  re-fires days later in a different workflow run (so
                   cross-run `actions/download-artifact` doesn't work).
                   This script walks the issue's comment history
                   newest-first, finds the most recent valid payload,
                   decodes, and writes it to
                   .rca/fix-proposals/<incident_id>.json so the
                   `fix-and-test` agent can load it via its normal
                   local-file path.

Why this is a standalone script instead of inline Python in the
workflow: we need to unit-test it. The handoff is the single most
fragile piece of the two-stage flow — a mangled base64 payload here
means a fix the human approved never reaches the PR stage, or worse,
a *different* fix silently gets realized.

Usage:
    python hydrate_proposal.py \\
        --issue 123 \\
        --repo owner/repo \\
        --out .rca/fix-proposals/123.json

Or, for offline testing, pipe the comments JSON (the same shape as
`gh api /repos/OWNER/REPO/issues/N/comments --paginate`) on stdin with
--stdin-comments:

    cat comments.json | python hydrate_proposal.py \\
        --issue 123 --repo owner/repo \\
        --out out.json --stdin-comments

Exit codes:
    0 — proposal hydrated successfully.
    1 — no proposal comment found on the issue.
    2 — a proposal comment was found but every payload failed to decode.
    3 — proposal decoded but failed integrity check (SHA-256 mismatch
        between the `diff` field and the `diff_sha256` field).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any, Iterable

# The marker pair that fences the base64-encoded proposal JSON inside a
# GitHub comment. These literals are duplicated in
# .claude/agents/fix-and-test.md (Flow A step 5 and Flow B step 1). If
# you change them here, change them there — and in
# sanitize_incident_body.py, which neutralizes these markers inside
# untrusted ticket bodies so a malicious reporter can't pre-populate a
# fake proposal.
MARKER_RE = re.compile(
    r"<!--RCA-PROPOSAL-JSON:START-->\s*(.*?)\s*<!--RCA-PROPOSAL-JSON:END-->",
    re.DOTALL,
)


def fetch_comments(repo: str, issue: str) -> list[dict[str, Any]]:
    """Fetch all comments on an issue via `gh api --paginate`."""
    raw = subprocess.check_output(
        ["gh", "api", "--paginate", f"repos/{repo}/issues/{issue}/comments"],
        text=True,
    )
    return json.loads(raw)


def extract_payloads(comment_body: str) -> Iterable[str]:
    """Yield every base64 payload found between marker pairs in a body.

    A single comment can legitimately contain at most one payload (the
    propose-only run posts exactly one marker pair). But a malicious
    actor could embed extra marker pairs in a later comment — we yield
    all of them and let the caller decide (it takes the first one that
    decodes cleanly).
    """
    for m in MARKER_RE.finditer(comment_body):
        # Strip whitespace inside the payload. GitHub's renderer wraps
        # long lines inside HTML comments sometimes; base64 tolerates
        # whitespace anywhere and we want to be permissive about it.
        yield "".join(m.group(1).split())


def decode_payload(payload_b64: str) -> dict[str, Any] | None:
    """Decode a single base64 payload. Returns None on any failure."""
    try:
        decoded = base64.b64decode(payload_b64, validate=True)
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None


def verify_integrity(proposal: dict[str, Any]) -> tuple[bool, str]:
    """Re-hash the `diff` field and compare to `diff_sha256`.

    Returns (ok, reason). If either field is missing we treat it as a
    soft failure (warn, but don't refuse) because older propose-only
    runs may have been written before this contract existed. Hard
    failure is only "both fields present and they disagree" — that's
    the tampering case.
    """
    diff = proposal.get("diff")
    pinned = proposal.get("diff_sha256")
    if not diff or not pinned:
        return True, "diff or diff_sha256 missing — skipping integrity check"
    actual = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    if actual != pinned:
        return False, f"diff_sha256 mismatch: pinned={pinned} actual={actual}"
    return True, "diff_sha256 matches"


def hydrate(
    comments: list[dict[str, Any]],
    issue: str,
    out_path: pathlib.Path,
) -> int:
    """Walk comments newest-first, hydrate the first valid proposal.

    Return the exit code: 0 ok, 1 no markers found at all, 2 markers
    found but all payloads failed to decode, 3 decoded but integrity
    check failed.
    """
    saw_markers = False
    saw_decode_success = False
    integrity_failure_reason = None

    for c in sorted(comments, key=lambda x: x.get("created_at", ""), reverse=True):
        body = c.get("body") or ""
        for payload in extract_payloads(body):
            saw_markers = True
            proposal = decode_payload(payload)
            if proposal is None:
                print(
                    f"::warning::Skipping malformed proposal payload "
                    f"in comment {c.get('id')}",
                    file=sys.stderr,
                )
                continue
            saw_decode_success = True
            ok, reason = verify_integrity(proposal)
            if not ok:
                # Keep walking — an earlier comment might have the
                # clean version and the newer one might be tampered.
                integrity_failure_reason = reason
                print(
                    f"::warning::Integrity check failed on comment "
                    f"{c.get('id')}: {reason}",
                    file=sys.stderr,
                )
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(proposal, indent=2))
            url = c.get("html_url", f"comment {c.get('id')}")
            print(f"Hydrated proposal from {url} into {out_path}")
            print(f"Integrity: {reason}")
            return 0

    if not saw_markers:
        print(
            f"::error::No proposal comment found on issue #{issue}; "
            f"cannot run open-pr stage.",
            file=sys.stderr,
        )
        return 1
    if not saw_decode_success:
        print(
            f"::error::Found proposal marker(s) on issue #{issue} but "
            f"every payload failed to decode. Payload corruption or "
            f"tampering.",
            file=sys.stderr,
        )
        return 2
    # We decoded at least one proposal but none passed integrity.
    print(
        f"::error::All decoded proposals on issue #{issue} failed "
        f"SHA-256 integrity check: {integrity_failure_reason}",
        file=sys.stderr,
    )
    return 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--stdin-comments",
        action="store_true",
        help="Read comments JSON array from stdin instead of calling gh",
    )
    args = ap.parse_args()

    if args.stdin_comments:
        comments = json.loads(sys.stdin.read())
    else:
        comments = fetch_comments(args.repo, args.issue)

    return hydrate(comments, args.issue, pathlib.Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
