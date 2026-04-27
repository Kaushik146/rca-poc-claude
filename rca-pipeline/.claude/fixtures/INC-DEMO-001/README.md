# INC-DEMO-001 — Inventory off-by-one on last unit

A canned incident used by the fixture harness (`scripts/fixture_harness.py`)
to regress the pipeline without hitting real MCP servers. The inputs are
pre-baked MCP responses; the expected outputs encode structural assertions
(shapes, invariants) — not byte-exact values — so the harness stays green
across prompt / model variations.

## Scenario

At 08:10 UTC, a deploy to `python-inventory-service` lands. At 08:30 the
`error_rate` metric begins climbing (CUSUM-detectable). At 08:51 PagerDuty
pages on-call. The user files a ticket at 09:00 saying *"checkout failing
for last-unit reserves — started this morning after the inventory deploy"*.

The root cause, pre-seeded in the demo service, is a known off-by-one on
the boundary check in `python-inventory-service/app.py` (`stock > quantity`
instead of `>=`). The prior-incident corpus contains a similar postmortem
from 2025 (`PM-42`) that's the expected top match.

## Files

| File              | Represents                                    |
|-------------------|-----------------------------------------------|
| `ticket.json`     | Atlassian MCP response for INC-DEMO-001       |
| `metrics.json`    | Datadog MCP response — error_rate / p99 series|
| `deploys.json`    | GitHub MCP response — merged PRs in lookback  |
| `pages.json`      | PagerDuty MCP response — incidents paged      |
| `candidates.json` | Confluence MCP response — postmortem search   |
| `expected.json`   | Structural assertions the harness checks      |

## How to run

```
make fixture        # runs scripts/fixture_harness.py against INC-DEMO-001
```

No credentials required — everything runs offline against the JSON in this
directory.
