# time-window-selector — script notes

## Invocation

```bash
cat payload.json | python3 .claude/skills/time-window-selector/scripts/select_window.py
```

The orchestrator passes `payload.json` as stdin. See `SKILL.md` for the input / output schema.

## Dependencies

- Python 3.11+
- `agents/algorithms/cusum.py` (already in the repo)
- No additional pip installs

## Offline by design

This script does not make any MCP calls itself. All MCP data (metric series, deploys, pages) is pre-fetched by the orchestrator / signals agent and passed in as JSON. This keeps the skill deterministic, testable, and reproducible.

## Testing

Drop a fixture JSON into `tests/` and pipe it in. Good fixtures to keep:
- `obvious_deploy_regression.json` — a single deploy + CUSUM upward shift right after
- `cascading_failure.json` — two pages on different services within 90 seconds
- `subtle_db_regression.json` — slow CUSUM drift, no deploy, no page
- `no_evidence.json` — only a ticket description with "since this morning"

Expected behaviour:
- First 3 → high confidence windows (>= 0.7)
- Fourth → low confidence, falls through to `fallback_window`

## Why this is not just a prompt

The window selection is a Bayesian combination of physical observations (CUSUM change-points, deploy events, pages). Running this in code — not in an LLM turn — means the same ticket + same MCP data produces the same window every run. That reproducibility is the enterprise trust story.
