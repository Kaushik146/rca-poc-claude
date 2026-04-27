---
name: time-window-selector
description: Given a free-text incident ticket with no explicit timestamp, returns ranked candidate time windows where the root cause likely occurred. Bayesian fusion over five priors — Datadog Watchdog / Dynatrace Davis anomalies (highest weight), CUSUM change-points, deploy events, paging events, and ticket-text anchors. The one unsolved step in the MCP chain - the reason this pipeline earns its keep beyond Claude Code + vendor AIOps alone.
---

# Time-Window Selector

## What this solves

Every MCP in the stack (Datadog, Dynatrace, GitHub, PagerDuty) will return data for whatever time range you ask for. None of them will **pick the right time range** from a vague incident ticket. That gap is the single highest-leverage piece of custom IP in the pipeline — and it's what Jothi flagged as "unsolved industry-wide."

Vendor AIOps (Datadog Watchdog, Dynatrace Davis) will happily flag "an
anomaly exists on error_rate between 08:30 and 08:50" — but it won't tell
you whether *this particular ticket* is about that anomaly, the deploy
twenty minutes earlier, or something that happened overnight. This skill
is the thing that takes the vendor AIOps flags and fuses them with the
other incident signals (deploys, pages, ticket text) to land on a single
ranked answer.

## When to invoke

Between the `intake` and `signals` phases, always. The signals agent needs a window; the ticket doesn't give one; this skill produces one.

## Input

```json
{
  "ticket": { ... intake output ... },
  "candidate_services": ["checkout-service", "payment-service"],
  "lookback_hours": 24,
  "metrics":          { "<service>": { "<metric>": [{"t": "...", "v": 0.01}, ...] } },
  "deploys":          [{"repo": "...", "sha": "...", "merged_at": "..."}],
  "pages":            [{"service": "...", "paged_at": "..."}],
  "vendor_anomalies": [{"service": "...", "metric": "...",
                        "severity": "HIGH",
                        "start": "...", "end": "...",
                        "source": "datadog_watchdog"}]
}
```

`vendor_anomalies` is where **Datadog Watchdog** and **Dynatrace Davis**
output lands. The signals agent pulls these first (before raw metrics)
and passes them through to this skill as the highest-weight prior. Jothi
was explicit: "they have an AIOps module" — we honor it.

## What to do

Run `scripts/select_window.py` via `Bash`. The script:

1. **Scores candidate windows** with a Bayesian combination over five priors (in descending weight):
   - Evidence 1 (**vendor AIOps** — Watchdog/Davis): each anomaly interval contributes a high-weight left-skewed bump centered on its midpoint. CRITICAL=1.3, HIGH=1.0, MEDIUM=0.7, LOW=0.4. Width scales with the anomaly span (capped at 25 min so a multi-hour flag doesn't smear the posterior).
   - Evidence 2 (**CUSUM change-points** via `agents/algorithms/cusum.py`): each detected regime shift contributes a Gaussian bump weighted by severity (CRITICAL=1.2 down to LOW=0.3). Fires when the vendor signal is absent or as independent confirmation.
   - Evidence 3 (**deploys** via GitHub MCP): right-skewed bump (incidents happen *after* the deploy, not before). Weight 0.7.
   - Evidence 4 (**pages** via PagerDuty MCP): left-skewed bump (pages fire *after* the incident starts). Weight 0.8.
   - Evidence 5 (**ticket-text anchors**): "this morning", "after lunch", "at 2pm" parsed to concrete UTC and biased with weight 0.3–0.8 depending on specificity.
2. **Returns top-3 windows** ranked by posterior probability, with rationales. Vendor anomaly hits are listed first in the rationale string so review is obvious.

## Output

```json
{
  "windows": [
    {
      "start": "2026-04-18T08:45:00Z",
      "end":   "2026-04-18T09:15:00Z",
      "confidence": 0.82,
      "rationale": "CUSUM upward shift in checkout-service.error_rate at 08:47 (severity HIGH). Deploy of payment-service sha=abc123 at 08:42. PagerDuty page at 08:51. Ticket reported 09:12.",
      "supporting_evidence": {
        "vendor_anomalies": [{"service": "inventory-service", "metric": "error_rate",
                              "severity": "HIGH", "start": "...", "end": "...",
                              "source": "datadog_watchdog"}],
        "change_points": [{"service": "checkout-service", "metric": "error_rate", "t": "...", "magnitude": 4.2}],
        "deploys": ["..."],
        "pages": ["..."]
      }
    }
  ],
  "fallback_window": {"start": "...", "end": "..."},
  "coverage_warning": null
}
```

## Confidence interpretation

- `>= 0.7` — high confidence. Signals agent uses this window directly.
- `0.4 - 0.7` — medium. Signals agent uses it but flags `window_confidence: medium` so the fix agent knows to be conservative.
- `< 0.4` — low. Orchestrator escalates to the user: "I can't narrow this down; please provide an approximate time."

## Why this beats "just ask the LLM to pick a window"

An LLM picking a window from a ticket description is doing free-text-to-timestamp guesswork with no access to the underlying metric data. This skill looks at the actual metric stream, finds where the regime actually shifted, and anchors the LLM's reasoning to physical observations. It's reproducible run-to-run on the same inputs, which a pure-LLM approach is not.

## Files in this skill

- `SKILL.md` — this file
- `scripts/select_window.py` — the implementation (wraps `agents/algorithms/cusum.py`)
- `scripts/README.md` — script-level docs and testing notes
