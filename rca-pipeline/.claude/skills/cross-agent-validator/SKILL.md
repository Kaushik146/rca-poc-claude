---
name: cross-agent-validator
description: Validates sub-agent output at every phase transition. Catches schema drift, missing fields, type errors, and contradictions between agents before they propagate. Wraps the existing agents/validation.py. The gate Claude Code doesn't have natively.
---

# Cross-Agent Validator

## Why this exists

Claude Code runs sub-agents independently. Nothing in the chassis catches the case where the log agent's output contradicts the trace agent's, or where a required field is missing, or where a timestamp is in a format the next agent won't parse. This skill is that gate.

## When to invoke

- The `validator` sub-agent calls this skill between every phase transition.
- Can also be invoked standalone to validate a single agent output.

## Input

```json
{
  "phase": "intake" | "signals" | "prior_incident" | "fix_and_test",
  "output": { ... the raw agent JSON ... },
  "prior_outputs": {        // optional context for cross-phase checks
    "intake": {...},
    "signals": {...}
  }
}
```

## What to do

Run `scripts/validate.py` via `Bash`. The script:

1. Dispatches to the right validator in `agents/validation.py` based on `phase`.
2. Runs schema-level and type-level checks.
3. If `prior_outputs` is present, runs cross-phase consistency checks:
   - `affected_components` in intake should overlap with services in signals
   - Error signatures in prior_incident top match should appear in signals logs
   - Files changed in fix_and_test should belong to services implicated by signals

## Output

```json
{
  "passed": true,
  "cleaned_output": {...},
  "normalizing_warnings": [...],
  "blocking_warnings": [...]
}
```

## Guardrails

- **Never invent data.** Missing fields default to `null` or `[]`; never synthesize plausible values.
- **Every transformation shows up in `normalizing_warnings`.** If the validator silently fixes a field, downstream debugging becomes impossible.
- **Cross-phase contradictions are blocking.** Do not let the orchestrator move on if the services implicated across phases disagree.
