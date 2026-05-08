# RCA Pipeline on Claude Code

**Author:** G. Kaushik Raj (Aspire Systems)
**Status:** Pre-pilot. Verified end-to-end against the bundled `INC-DEMO-001` fixture in dry-run propose-only mode. 57-test offline regression suite green.

A proof of concept for an automated incident root cause analysis pipeline built on the Claude Code chassis. Reads an incident ticket, pulls observability signals from a defensibly chosen time window, finds the closest prior incident, proposes a minimal code fix, runs the test suite, and posts the result as a comment on the issue. Never opens a PR without a human approval label. Never merges.

---

## What it does

Four capabilities, in order:

1. **Intake.** Reads the incident ticket from Jira (or Azure DevOps) and the affected service's spec. Routes to the right module if `affected_components` is empty.
2. **Signals.** Pulls vendor AIOps anomalies first (Datadog Watchdog, Dynatrace Davis), then logs, traces, metrics, deploys, and pages from the chosen time window.
3. **Prior incident.** BM25 retrieval over Confluence postmortems and PagerDuty resolved incidents to find the closest historical match.
4. **Fix and test.** Generates a minimal code diff, runs the test suite, computes a SHA-256 integrity pin on the diff, and posts a proposal comment. A second run, triggered by a human label, opens the PR.

A validator runs between every phase and the orchestrator never skips it.

---

## Architecture

```
/rca INC-1234
   |
   +-- orchestrator (sonnet)
          |
          +-- intake (haiku)         -> Jira / ADO MCP
          |     +-- module-router skill
          |
          +-- time-window-selector skill   (custom IP)
          |
          +-- signals (haiku)        -> Datadog / Dynatrace / PagerDuty / GitHub MCP
          |     +-- anomaly-ensemble skill   (fallback only)
          |
          +-- prior-incident (haiku) -> Confluence / ADO Wiki / PagerDuty MCP
          |     +-- bm25-rerank skill
          |
          +-- fix-and-test (sonnet)  -> GitHub MCP
          |
          +-- validator (haiku) runs between every phase
                +-- cross-agent-validator skill
```

Sub-agents live under `.claude/agents/`. Skills live under `.claude/skills/`. The `/rca` slash command lives at `.claude/commands/rca.md`. MCP server connection definitions live at `.mcp.json` at the repo root.

---

## Custom IP: time-window-selector

The one chunk of real custom logic in the repo. Combines five priors as log-probabilities:

- Vendor AIOps anomalies (Datadog Watchdog, Dynatrace Davis), severity-scaled (CRITICAL 1.3, HIGH 1.0, MEDIUM 0.7, LOW 0.4). Highest-weight prior.
- CUSUM change-points on relevant metric series.
- Deploy priors (right-skewed Gaussian centered on each recent deploy).
- Page priors (left-skewed Gaussian centered on each PagerDuty page).
- Ticket-text anchors ("this morning", "after lunch", "at 2pm") parsed to UTC ranges.

Returns the top 3 non-overlapping 30-minute windows with confidence. If top confidence is below 0.4, the orchestrator stops and asks the user. It does not guess.

The Watchdog-first contract is wired in three places: signals pulls vendor anomalies before any other obs query, the selector treats them as the top-weight prior, and the anomaly-ensemble skill refuses to run when vendor anomalies were non-empty.

---

## Quick start

Three layers. Pick whichever matches what you want to verify.

### Layer 1: 5-second sanity check (no creds, no LLM)

```
make install
make ci-chassis
```

Runs the entire 57-test offline regression suite: fixture harness, handoff round-trip with SHA-256 verification, 30-case prompt-injection corpus, chassis contract tests, CLI smoke test, seeded-bug round-trip. If this is green, the chassis is structurally sound.

### Layer 2: Dry-run shakedown with Claude Code (no creds, real LLM)

Exercises the full agent topology end-to-end against the bundled `INC-DEMO-001` fixture. No MCP credentials needed because `RCA_DRY_RUN=1` substitutes fixture data for every MCP call.

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export RCA_DRY_RUN=1
export RCA_STAGE=propose-only
export RCA_REQUIRE_FIX_APPROVAL=1

claude
```

Inside Claude Code:

```
/rca INC-DEMO-001
```

Expected runtime: 60 to 90 seconds. Output lands at `.rca/fix-proposals/INC-DEMO-001.json` (the proposal payload pinned by SHA-256) and `.rca/reports/INC-DEMO-001-<timestamp>.json` (the per-phase trace). No PR opens. No branch pushes.

### Layer 3: Live on a real incident (creds required)

See the **Live mode setup** section below. Run locally for one-off shakedowns, or open a labeled GitHub issue to trigger the production workflow.

---

## Live mode setup

The chassis reads credentials from environment variables. `.mcp.json` resolves them via `${VAR}` substitution.

### Required environment variables

| Variable | Service | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude API (only needed for the GitHub Actions runner; local `claude` uses your Claude Code login) | console.anthropic.com/settings/keys |
| `ATLASSIAN_OAUTH_TOKEN` | Jira and Confluence (Atlassian Rovo MCP) | admin.atlassian.com after Rovo MCP install |
| `DD_API_KEY` and `DD_APP_KEY` | Datadog MCP | datadoghq.com -> Organization Settings -> API Keys + Application Keys |
| `DT_ENV_URL` and `DT_API_TOKEN` | Dynatrace MCP | Your tenant -> Access tokens. Token needs `metrics.read`, `entities.read`, `events.read`, `problems.read`, `DataExport`. |
| `GITHUB_PAT` | GitHub MCP | github.com/settings/tokens. Fine-grained PAT with `repo`, `read:org`, `read:user`. |
| `PAGERDUTY_API_TOKEN` | PagerDuty MCP | User icon -> My Profile -> User Settings -> Create API User Token |
| `AZURE_DEVOPS_ORG_URL` and `AZURE_DEVOPS_PAT` | Azure DevOps MCP (only if using ADO instead of Jira) | dev.azure.com -> User settings -> Personal access tokens. Needs `Code: read`, `Work items: read`, `Wiki: read`. |

### Running locally with real creds

Drop the variables in a `.env` file at the repo root (already covered by `.gitignore`):

```
ANTHROPIC_API_KEY=sk-ant-...
ATLASSIAN_OAUTH_TOKEN=...
DD_API_KEY=...
DD_APP_KEY=...
DT_ENV_URL=https://abc12345.live.dynatrace.com
DT_API_TOKEN=...
GITHUB_PAT=...
PAGERDUTY_API_TOKEN=...
```

Then:

```
source .venv/bin/activate
set -a; source .env; set +a

unset RCA_DRY_RUN
export RCA_STAGE=propose-only
export RCA_REQUIRE_FIX_APPROVAL=1

claude
```

Inside Claude Code: `/rca INC-XXXX` where `INC-XXXX` is a real Jira or ADO ticket ID. The chassis will hit live MCPs, post a real proposal comment on the GitHub issue, and stop. No PR opens until a human applies the `rca:approve-fix` label.

### Running via GitHub Actions

Repo -> Settings -> Secrets and variables -> Actions -> New repository secret. Add each of the variables above. The workflow at `.github/workflows/rca.yml` reads them automatically.

Trigger paths:

- Open a GitHub issue with the `incident` label -> workflow runs in `propose-only` mode.
- Apply the `rca:approve-fix` label to that issue -> workflow re-fires with `RCA_STAGE=open-pr` and opens the PR.
- Apply `rca:reject` -> workflow no-ops.
- `workflow_dispatch` -> manual replay with a chosen `rca_stage`.

The proposal JSON is durably stored as a base64 payload inside the proposal comment on the incident issue, fenced by `<!--RCA-PROPOSAL-JSON:START-->` and `<!--RCA-PROPOSAL-JSON:END-->`. The open-pr stage extracts it, verifies the SHA-256 matches, re-runs the tests at `HEAD`, and only then opens the PR.

---

## Service-to-repo resolution in live mode

`fix-and-test` does not have a hard-coded mapping from service name to GitHub repo. The mapping flows through the chassis from the upstream `signals` phase. The `signals` agent emits a `deploys` array, each entry shaped:

```json
{ "repo": "org/inventory-service", "sha": "7c4f9a2", "merged_at": "...", "pr_url": "..." }
```

This array surfaces every recent deploy to a candidate service inside the selected time window, sourced via `mcp__github__*`. `fix-and-test` reads each entry's `repo` and uses the GitHub MCP to fetch the relevant files, generate the diff, and open the PR against that repo's default branch.

The implication for live setup: the GitHub PAT used by the chassis must have access to every repo that hosts a candidate service. If a service lives in a private repo the PAT can't see, the deploys query for that service returns empty and the fix can't be generated. The chassis logs `coverage: insufficient_repo_access` rather than guessing the repo.

The demo services under `java-order-service/`, `node-notification-service/`, and `python-inventory-service/` are co-located with the chassis so the fixture harness can exercise this end-to-end without needing live GitHub MCP access. They do NOT model how a real deployment is shaped.

---

## Production safety controls

Three environment variables form the production contract. Treat them as non-negotiable, not optional configuration.

| Variable | Default | Effect |
|---|---|---|
| `RCA_DISABLED` | `0` | When `1`, `/rca` exits immediately. No MCP calls. Global kill switch. Set as a GitHub repo variable so ops can flip it without a commit. |
| `RCA_REQUIRE_FIX_APPROVAL` | `1` | When `1`, `fix-and-test` runs in `propose-only` mode by default and refuses to run as `full`. The HITL gate. |
| `RCA_STAGE` | `propose-only` | Valid values: `propose-only`, `open-pr`, `full`. The first is the production default; the second is fired by the approval label; the third is dev/pilot only. |
| `RCA_DRY_RUN` | `0` | When `1`, agents read fixture data instead of calling MCPs and refuse to open a PR. Used for the pre-pilot shakedown. |
| `RCA_PR_REVIEWERS` | `copilot` | Comma-separated GitHub handles assigned to PRs opened by `fix-and-test`. Configurable per org. |

Stage flow:

| Trigger | Resulting stage | Behavior |
|---|---|---|
| Issue opened with `incident` | `propose-only` | Analyze, post proposal comment, stop |
| Label `rca:approve-fix` added | `open-pr` | Re-verify, open PR, stop. Never merges. |
| Label `rca:reject` added | (no-op) | Workflow no-ops. Proposal expires per artifact retention. |
| `workflow_dispatch` (manual) | operator choice | Replay with chosen stage |
| `RCA_DISABLED=1` | (any) | Exit cleanly without MCP calls |

---

## Verification

The chassis is exercised by 57 offline tests. Run individually or as a group:

| Target | What it covers |
|---|---|
| `make verify-mcp` | `.mcp.json` structure check |
| `make fixture` | Fixture harness against `INC-DEMO-001`. 6 fatal checks: module routing, time-window selection, vendor-anomaly fusion, BM25 retrieval, anomaly ensemble, cross-agent validation. |
| `make test-handoff` | Handoff round-trip: encode -> render -> hydrate -> SHA-256 verify. Covers `-->`-in-diff escape, unicode, tampering detection. |
| `make test-sanitize` | 30-case prompt-injection corpus on the incident body sanitizer. |
| `make test-chassis-contracts` | Selector uncertainty escalation, severity weighting, GitHub comment-size ceiling, prose-lint of guardrail tokens in `rca.md`. |
| `make smoke-cli` | Claude Code CLI flag and topology smoke test (would have caught the three flag bugs we shipped during pre-pilot). |
| `make shakedown` | Topology dry-run end-to-end against the fixture. No live creds. |
| `make ci-chassis` | All of the above plus the seeded-bug round-trip. |

GitHub Actions runs the same gate on every PR via `.github/workflows/fixture.yml`.

---

## Project layout

```
.
+-- .claude/
|   +-- agents/        intake, signals, prior-incident, fix-and-test, validator, orchestrator
|   +-- commands/      /rca slash command definition
|   +-- fixtures/      INC-DEMO-001 fixture data for offline runs
|   +-- skills/        time-window-selector (headline IP), bm25-rerank,
|                      anomaly-ensemble, cross-agent-validator, module-router
+-- .github/workflows/
|   +-- rca.yml        production workflow (issue label trigger)
|   +-- fixture.yml    CI regression gate on every PR
+-- .mcp.json          MCP server connection definitions
+-- agents/            legacy GPT-4o pipeline + algorithms/ (CUSUM, BM25, etc.)
+-- scripts/           harness, sanitizer, hydrator, smoke test, tests/
+-- java-order-service/         demo service for the test harness
+-- python-inventory-service/   demo service (the seeded-bug target)
+-- node-notification-service/  demo service
+-- CLAUDE.md          loaded by Claude Code at session start
+-- RUNBOOK.md         partial-failure procedures, on-call playbook
+-- SECURITY.md        threat model, MCP scope review, prompt-injection mitigation
+-- MCP_Landscape_for_RCA.md   research: MCP coverage per vendor
```

---

## Read more

- `CLAUDE.md` covers the runtime topology, model tiering rationale, skills convention, and the Watchdog-first contract in detail. Auto-loaded by Claude Code.
- `RUNBOOK.md` covers partial-failure procedures, what to do when the chassis posts a wrong proposal, kill-switch escalation, and the pre-pilot shakedown checklist.
- `SECURITY.md` covers the threat model, MCP scope review, and the prompt-injection mitigation in `scripts/sanitize_incident_body.py`.
- `MCP_Landscape_for_RCA.md` is the research note on which MCPs cover what, and where self-hosted alternatives exist for VPC-only data.

---

## Out of scope

This pipeline does four things and refuses to do anything else. The following are explicitly out of scope and should be bought from vendors:

- Triage routing and on-call scheduling (PagerDuty does this)
- Customer comms during incidents (Statuspage)
- Sentiment analysis or NLP on tickets (Zendesk, Forethought)
- Dashboarding (Grafana, Datadog dashboards)
- Sev classification heuristics (FireHydrant, Incident.io)

The pipeline is one slice. It does that slice well.
