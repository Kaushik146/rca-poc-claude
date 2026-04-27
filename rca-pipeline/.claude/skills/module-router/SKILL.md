---
name: module-router
description: Routes a vague incident ticket ("checkout is broken") to the most likely service and requirement doc. Used by the intake agent when the ticket doesn't name a specific service. Combines CODEOWNERS + service topology + Confluence tags.
---

# Module Router

## Why this exists

Tickets often say "checkout is broken" without naming the owning service. The intake agent calls this skill to map vague descriptions to concrete services — so the signals agent can scope metric queries, and the prior-incident agent can search the right requirement docs.

## When to invoke

`intake` agent invokes this when `affected_components` is empty after parsing the ticket.

## Input

```json
{
  "ticket_text": "checkout is broken for customers in EU",
  "known_services": ["checkout-service", "payment-service", "inventory-service", "notification-service"]
}
```

## What to do

Run `scripts/route.py` via `Bash`. The script:

1. Tokenizes the ticket text.
2. Scores each known service by:
   - Literal name match (highest weight)
   - Synonym match from a per-repo routing table (`CLAUDE.md` can declare these)
   - CODEOWNERS keyword overlap (if `CODEOWNERS` lists keywords per path)
3. Returns the top 3 services with scores.

## Output

```json
{
  "routed": [
    {"service": "checkout-service", "score": 0.92, "reason": "literal match"},
    {"service": "payment-service", "score": 0.31, "reason": "synonym: checkout → payment"}
  ]
}
```

## Guardrails

- **If the top score is < 0.3, return an empty list and flag `needs_disambiguation: true`.** Do not force-pick a service the text doesn't support. The orchestrator will ask the user.
- **Route to services actually in `known_services`.** Never invent a service name.
