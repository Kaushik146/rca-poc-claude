#!/usr/bin/env python3
"""
PII scrubber — strips emails, API keys, tokens, and other sensitive
patterns from log lines, trace messages, and arbitrary JSON before
that data flows into an LLM prompt.

Called by the `signals` agent (via Bash) on every batch of logs/traces
it pulls from Datadog / Dynatrace before emitting them to its
structured output. Downstream agents (prior-incident, fix-and-test)
never see the raw observability data — only the scrubbed version.

What it scrubs (conservative on purpose — false positives mangle
observability data which is worse than rare PII shapes slipping through):
  - email addresses           → <EMAIL>
  - JWT tokens (eyJ...)       → <JWT>
  - Bearer/Authorization      → <BEARER_TOKEN>
  - common API key prefixes   → <API_KEY>
    (sk-..., ghp_/gho_/ghs_/ghu_..., xoxb-/xoxp-..., pat-...)
  - AWS access keys (AKIA...) → <AWS_KEY>
  - SSN (US: NNN-NN-NNNN)     → <SSN>
  - credit card numbers       → <CC>
  - IPv4 addresses            → <IP>
  - phone numbers (US-ish)    → <PHONE>

Explicitly NOT scrubbed: UUIDs / trace IDs, Git SHAs, URLs without
embedded credentials, generic base64 blobs, version strings.

Usage:
  python3 scripts/pii_scrubber.py < input.json > output.json
  python3 scripts/pii_scrubber.py --input @- --output @-
  python3 scripts/pii_scrubber.py --scrub-string "raw text"

Emits structured stats on stderr (e.g. "scrubbed: emails=3 jwts=1") so
the calling agent can capture them as pii_scrub_stats for audit trail.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("jwts", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<JWT>"),
    ("bearer", re.compile(r"\b(?:Bearer|Authorization:?\s*Bearer)\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "<BEARER_TOKEN>"),
    ("aws_keys", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<AWS_KEY>"),
    ("api_keys", re.compile(
        r"\b("
        r"sk-[A-Za-z0-9]{20,}"
        r"|sk_live_[A-Za-z0-9]{12,}"
        r"|sk_test_[A-Za-z0-9]{12,}"
        r"|gh[pousr]_[A-Za-z0-9]{20,}"
        r"|xox[bpoas]-[A-Za-z0-9\-]{12,}"
        r"|pat-[a-z0-9]{8,}-[a-z0-9\-]{20,}"
        r"|dop_v1_[A-Za-z0-9]{40,}"
        r")\b"
    ), "<API_KEY>"),
    ("emails", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<EMAIL>"),
    ("credit_cards", re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "<CC>"),
    ("ssns", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "<SSN>"),
    ("ips", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"), "<IP>"),
    ("phones", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "<PHONE>"),
]


def scrub_string(text: str, counts: dict[str, int] | None = None) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for name, pattern, replacement in _PATTERNS:
        out, n = pattern.subn(replacement, out)
        if counts is not None and n:
            counts[name] = counts.get(name, 0) + n
    return out


def scrub_obj(obj: Any, counts: dict[str, int] | None = None) -> Any:
    """Walk JSON-like structure; scrub string values. Keys NOT scrubbed."""
    if isinstance(obj, str):
        return scrub_string(obj, counts)
    if isinstance(obj, dict):
        return {k: scrub_obj(v, counts) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_obj(v, counts) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_obj(v, counts) for v in obj)
    return obj


def _read_in(arg: str) -> str:
    if arg == "@-":
        return sys.stdin.read()
    from pathlib import Path
    return Path(arg).read_text()


def _write_out(arg: str, content: str) -> None:
    if arg == "@-":
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    else:
        from pathlib import Path
        Path(arg).write_text(content)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PII scrubber for the RCA chassis")
    ap.add_argument("--input", default="@-")
    ap.add_argument("--output", default="@-")
    ap.add_argument("--scrub-string", default=None)
    ap.add_argument("--no-stats", action="store_true")
    args = ap.parse_args(argv)

    counts: dict[str, int] = {}

    if args.scrub_string is not None:
        sys.stdout.write(scrub_string(args.scrub_string, counts) + "\n")
    else:
        raw = _read_in(args.input)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = scrub_string(raw, counts)
            _write_out(args.output, data)
            if not args.no_stats:
                sys.stderr.write("scrubbed (text mode): " +
                                 " ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")
            return 0
        scrubbed = scrub_obj(data, counts)
        _write_out(args.output, json.dumps(scrubbed, indent=2, sort_keys=False))

    if not args.no_stats:
        if counts:
            sys.stderr.write("scrubbed: " +
                             " ".join(f"{k}={v}" for k, v in sorted(counts.items())) + "\n")
        else:
            sys.stderr.write("scrubbed: (no matches)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
