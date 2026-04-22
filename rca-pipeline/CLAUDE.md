# CLAUDE.md — Context for Claude Code

This file is loaded automatically by Claude Code at the start of every session.
It tells Claude how this repo is organized, what the deterministic algorithms
under `agents/algorithms/` are for, and which conventions the RCA pipeline
depends on.

## What this project is

An **incident RCA pipeline** that:

1. Pulls an incident ticket + the affected service's requirement doc.
2. Pulls observability signals (logs, traces, metrics, deploys, pages) for a
   **defensibly chosen time window**.
3. Finds the closest prior incident (postmortems + past tickets).
4. Proposes a minimal code fix, verifies it against the existing test suite,
   and opens a PR — never merges.

The runtime chassis is **Claude Code**. Enterprise data lives in first-party
MCP servers (Jira/Confluence via Atlassian Rovo, Azure DevOps, Datadog,
Dynatrace, PagerDuty, GitHub). The only custom code is the narrow slice that
those MCPs do not cover — most notably **time-window selection from vague
tickets**, which is still an open problem industry-wide.

## Repo layout

```
.
├── .claude/
│   ├── agents/          # sub-agents with model tiering (haiku / sonnet)
│   ├── skills/          # deterministic building blocks (python wrappers)
│   └── commands/        # slash commands (/rca)
├── .github/workflows/   # GitHub Action that runs /rca on labeled incidents
├── .mcp.json            # MCP server configuration (placeholders for secrets)
├── agents/
│   ├── algorithms/      # pure-python IP — CUSUM, BM25, anomaly ensemble
│   └── validation.py    # cross-agent validators
├── java-order-service/          # demo services used in the test harness
├── node-notification-service/
├── python-inventory-service/
├── MCP_Landscape_for_RCA.md     # research: what MCP covers vs where we build
└── requirements.txt
```

## Runtime topology

```
/rca INC-1234
   │
   └── orchestrator (sonnet)
          ├── intake (haiku)          ──→  Atlassian / ADO MCP
          │   └── module-router skill (if affected_components empty)
          │
          ├── time-window-selector skill   (custom IP — Bayesian fusion)
          │
          ├── signals (haiku)         ──→  Datadog / Dynatrace / PagerDuty / GitHub MCP
          │   └── anomaly-ensemble skill   (fallback when vendor AIOps empty)
          │
          ├── prior-incident (haiku)  ──→  Confluence / ADO Wiki / PagerDuty MCP
          │   └── bm25-rerank skill
          │
          ├── fix-and-test (sonnet)   ──→  GitHub MCP
          │
          └── validator (haiku)  runs between every phase
              └── cross-agent-validator skill
```

The sequence is fixed (validator sits between every pair of phases). The
orchestrator never skips the validator.

## Conventions for sub-agents

- **Model tiering is deliberate.** Collectors and the validator use `haiku`.
  The orchestrator, time-window-selector skill, and `fix-and-test` use
  `sonnet`. Do not upgrade a collector to sonnet without a reason — the
  whole design assumes high-volume, low-cost inference for data pulls.

- **Sub-agents return structured JSON, never prose summaries**, so the
  validator can diff them.

- **Every MCP write is guarded.** `fix-and-test` opens PRs but never merges
  them. No agent is allowed to close a ticket, page-out an engineer, or
  mutate a dashboard.

## Conventions for skills

Skills live under `.claude/skills/<name>/` and have:

- `SKILL.md` — name, description, input schema, output schema, guardrails.
- `scripts/` — Python (or whatever) that does the actual work.

The scripts import from `agents/algorithms/` by putting `agents/` on
`sys.path` (matching the project's existing convention — every `agents/*.py`
file does `from algorithms.X`, so `agents/` is expected to be on PYTHONPATH
and `algorithms/` resolves as a top-level package). That keeps the existing
Python IP untouched — skills are the interface layer, not a rewrite.
Pattern:

```python
HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[4]   # skills/<name>/scripts/x.py → repo root
sys.path.insert(0, str(REPO_ROOT / "agents"))
from algorithms.cusum import CUSUMDetector
```

## The one non-obvious piece: time-window selection

If you only read one part of this repo, read
`.claude/skills/time-window-selector/SKILL.md`. Most of the value of this
pipeline comes from this skill. Vendor AIOps (Watchdog, Davis AI) gives you
"here is an anomaly"; it does **not** give you "here is the 30-minute window
the user actually meant when they wrote 'checkout is slow this morning'".

The skill combines five priors, in descending weight:

- **Vendor AIOps anomalies** (Datadog Watchdog / Dynatrace Davis) —
  highest weight. Severity-scaled (CRITICAL 1.3, HIGH 1.0, MEDIUM 0.7,
  LOW 0.4), left-skewed around the anomaly midpoint, width scales with
  the span (capped 25 min). The `signals` agent pulls these first and
  feeds them through to this skill as a first-class input field.
- **CUSUM change-points** on relevant metric series (data-driven, fires
  even when vendor AIOps is absent).
- **Deploy priors**, right-skewed Gaussian centered on each recent deploy
  (regressions lag the deploy by minutes-to-hours).
- **Page priors**, left-skewed Gaussian centered on each PagerDuty page
  (pages trail the first symptom).
- **Ticket-text anchors** ("this morning", "after lunch", "at 2pm")
  parsed to concrete UTC ranges.

All five priors are combined as log-probabilities and the top 3 non-overlapping
30-minute windows are returned with confidence scores. If the top confidence
is `< 0.4`, the orchestrator stops and asks the user for a time — it does
**not** guess.

### Watchdog-first contract

Jothi was explicit during review: "they have an AIOps module, honor it."
That instruction is wired into the chassis, not just the docs:

- `signals` agent pulls `vendor_anomalies` *before* any other
  observability query, emits them as a structured first-class output
  field, and sets `ensemble_fallback_used: true` only when that array is
  empty for every candidate service.
- `time-window-selector` takes `vendor_anomalies` as its highest-weight
  prior (see above) and lists vendor hits first in the rationale string.
- `anomaly-ensemble` is fallback-only — it refuses to run when upstream
  `vendor_anomalies` was non-empty (double-counting Watchdog output
  would downgrade a trained, labelled signal to an unlabelled one).
- The fixture harness has a `time_window_selector_vendor` check that
  re-runs the selector with and without `vendor_anomalies.json` fed in,
  asserting (a) target timestamp stays in the top window, (b) Watchdog
  evidence surfaces in `supporting_evidence`, and (c) confidence does
  not regress vs the no-vendor baseline. This is the gate that proves
  the wiring actually fuses vendor anomalies into the posterior.

## Conventions for commits / PRs opened by `fix-and-test`

- Title: `fix(<service>): <one-line hypothesis>`
- Body includes: the failing test that now passes, the RCA report ID, and
  a link to the prior-incident match (if any).
- Branch name: `rca/<incident-id>`.
- Reviewers: read from the `RCA_PR_REVIEWERS` env var (comma-separated
  GitHub handles, no `@` prefix). Default `copilot` — the original
  "second pair of eyes before a human reviewer looks at it" convention.
  Orgs using a different review bot or a named team set the variable at
  the workflow/repo level; never hardcode.
- **Never** use `--no-verify`, `--amend`, or `gh pr merge`.

## Kill switch, staged execution, human-in-the-loop

The pipeline honors three environment variables. These are the production
safety controls; treat them as non-negotiable contract, not optional
configuration.

- `RCA_DISABLED=1` — global kill switch. When set, the `/rca` command and
  the orchestrator exit immediately without making any MCP call. Use when
  the pipeline is misbehaving and you want it off *now*, before anyone
  can re-trigger it. Configured as a GitHub repository variable so ops can
  flip it without a commit.

- `RCA_REQUIRE_FIX_APPROVAL=1` — human-in-the-loop gate (production
  default, set at the workflow level). When on, the fix-and-test agent
  runs in `propose-only` mode: it generates the fix, runs tests, posts a
  comment on the incident ticket with the hypothesis + diff + test
  results, and stops. No PR opens until a human applies the
  `rca:approve-fix` label to the incident issue, which re-fires the
  workflow with `RCA_STAGE=open-pr`. Set to `0` only in dev/pilot where
  auto-PR is acceptable, and document the exception.

- `RCA_STAGE` — which phase to run. Valid values: `full` (all phases,
  including auto-PR; only honored when HITL gate is off), `propose-only`
  (stops after posting the proposal comment — the production default when
  HITL is on), `open-pr` (skip the analysis phases, read the saved
  proposal JSON, verify approval, open the PR).

The flow matrix:

| Trigger                         | RCA_STAGE       | Behavior                              |
| ------------------------------- | --------------- | ------------------------------------- |
| Issue opened w/ `incident`      | `propose-only`  | Analyze + post proposal comment       |
| Label `rca:approve-fix` added   | `open-pr`       | Re-verify + open PR                   |
| Label `rca:reject` added        | (no-op)         | Workflow no-op; proposal artifact expires |
| `workflow_dispatch` (manual)    | operator choice | Replay with stage=X                   |
| `RCA_DISABLED=1`                | any             | Exit cleanly, no MCP calls            |

The contract between the two stages is the file
`.rca/fix-proposals/<incident_id>.json`, which the propose-only stage
writes and uploads as an artifact; the open-pr stage downloads and
applies. The diff is pinned in that file — the open-pr stage is not
allowed to regenerate the fix, only realize the one that was approved.

## Testing

The demo services under `java-order-service/`, `node-notification-service/`,
and `python-inventory-service/` exist so the RCA pipeline can be exercised
end-to-end against realistic repos. `docker-compose.yml` brings them up.
`Makefile` has targets for running synthetic incidents.

## What NOT to do

- **Do not replace Claude Code's built-in primitives with custom Python.**
  If Claude Code already does planning, tool-calling, retries, or context
  management, use it — the custom Python here is the narrow slice where
  Claude Code + MCPs do not cover the domain.

- **Do not build a vector database for prior incidents.** BM25 over a
  bounded candidate set (Confluence/ADO Wiki/PagerDuty postmortems) is
  faster, cheaper, and explainable. See `.claude/skills/bm25-rerank/`.

- **Do not widen the scope past the four capabilities above.** Triage
  routing, sentiment, customer comms, on-call scheduling, and dashboarding
  are explicitly out of scope — enterprises buy those from vendors.
