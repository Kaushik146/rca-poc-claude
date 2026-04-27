#!/usr/bin/env python3
"""
module-router — heuristic mapping from vague ticket text to service name.

Input  (stdin JSON): see SKILL.md
Output (stdout JSON): {"routed": [...], "needs_disambiguation": bool}

Deliberately simple: name + synonym + keyword scoring. No LLM call.
A more sophisticated version can layer embeddings on top, but the
"literal + synonym + CODEOWNERS" baseline handles most real tickets.
"""
from __future__ import annotations
import json
import re
import sys
from collections import Counter

STOP = {"is", "the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "at",
        "not", "with", "was", "are", "be", "been", "being", "that", "this", "it", "we", "i"}

# Very short synonym table — users can extend this in CLAUDE.md if needed.
SYNONYMS: dict[str, set[str]] = {
    "checkout":      {"cart", "basket", "purchase", "buy", "order"},
    "payment":       {"pay", "charge", "billing", "stripe", "card"},
    "inventory":     {"stock", "warehouse", "sku", "availability"},
    "notification":  {"email", "sms", "push", "alert", "notify"},
    "auth":          {"login", "signin", "signup", "oauth", "sso", "token"},
    "search":        {"query", "lookup", "find"},
}


def tokenize(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP and len(t) > 2]


def service_keywords(service: str) -> set[str]:
    """Extract keywords a service name maps to, using the synonym table."""
    base = service.lower().replace("-service", "").replace("_", "-").split("-")
    kws: set[str] = set(base)
    for b in base:
        kws |= SYNONYMS.get(b, set())
    return {k for k in kws if len(k) > 2}


def main() -> None:
    payload = json.loads(sys.stdin.read())
    text = payload.get("ticket_text", "") or ""
    known = payload.get("known_services", []) or []
    tokens = tokenize(text)
    tc = Counter(tokens)

    scored: list[dict] = []
    for svc in known:
        kws = service_keywords(svc)
        literal_hits = sum(tc[k] for k in kws if k in svc.lower())
        synonym_hits = sum(tc[k] for k in kws if k not in svc.lower())
        # Weighting: literal name tokens are worth more than synonyms
        score = (1.0 * literal_hits) + (0.5 * synonym_hits)
        if score == 0: continue
        # Normalize roughly to [0, 1] for readability
        norm = min(1.0, score / max(1.0, sum(tc.values()) / 2.0))
        reason = "literal match" if literal_hits else f"synonym: {[k for k in kws if k in tokens]}"
        scored.append({"service": svc, "score": round(norm, 3), "reason": reason})

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:3]
    needs_disamb = (not top) or (top[0]["score"] < 0.3)

    print(json.dumps({
        "routed": top if not needs_disamb else [],
        "needs_disambiguation": needs_disamb,
    }, indent=2))


if __name__ == "__main__":
    main()
