# RCA Pipeline on Claude Code — Architecture Note

> This is the short, senior-facing explanation of what changed after the
> conversation with Jothi and Uma. It is intentionally terse. For the
> long-form version, read `CLAUDE.md` + `MCP_Landscape_for_RCA.md`.

## The shift

The original pipeline was 18 custom Python agents across 5 phases, with its
own orchestrator, its own validation layer, its own retry logic, its own
"model tier" router, and its own 53-test harness. That framing loses to
Claude Code + Datadog AIOps + Dynatrace Davis the moment a CIO asks
"why not buy?".

**New framing.** Claude Code is the chassis. The IP is only what Claude
Code + MCPs cannot do yet. Everything else — orchestration, sub-agent
model tiering, retries, tool-calling, context management — is handed off
to the chassis.

## What collapsed into the chassis

| Original custom component             | Replaced by                                |
| ------------------------------------- | ------------------------------------------ |
| Orchestrator agent                    | `.claude/agents/orchestrator.md` (sonnet)  |
| Ticket-fetcher, Jira/ADO clients      | Atlassian Rovo MCP + ADO MCP               |
| Log/trace/metric collectors (×6)      | Datadog MCP + Dynatrace MCP                |
| Deploy-timeline collector             | GitHub MCP                                 |
| Pages collector                       | PagerDuty MCP                              |
| Prior-incident vector search          | Confluence MCP + BM25 rerank (no vectors)  |
| Fix-generation agent                  | `.claude/agents/fix-and-test.md` (sonnet)  |
| Custom "model tier router"            | Claude Code sub-agent `model:` frontmatter |
| Custom retry / fallback               | Claude Code primitives                     |
| 53-test harness for agents            | GitHub Action running `/rca` on a fixture  |

## What survived as custom IP (and why)

Four skills, each wrapping code that already existed in `agents/algorithms/`:

1. **`time-window-selector`** — the single highest-leverage piece. Fuses
   five priors into a Bayesian score over 30-minute windows, in descending
   weight: **Datadog Watchdog / Dynatrace Davis anomalies** (highest), CUSUM
   change-points, deploy events, paging events, and ticket-text anchors.
   Jothi explicitly called this out as unsolved industry-wide — vendor
   AIOps tells you "there was an anomaly on error_rate at 08:35," but it
   does *not* tell you whether this particular ticket is about that
   anomaly, the deploy twenty minutes earlier, or something overnight.
   This skill is the thing that picks the window. No vendor does this.

2. **`bm25-rerank`** — deterministic reranker over Confluence / ADO Wiki /
   PagerDuty postmortems. A vector DB would be overkill, harder to explain,
   and worse at incident jargon ("P0", "503", "OOMKilled") that tokenizes
   badly.

3. **`anomaly-ensemble`** — autoencoder + isolation forest, used **only as
   fallback** when Datadog Watchdog or Dynatrace Davis returns nothing.
   The precedence rule is a hard contract: the signals agent pulls
   `vendor_anomalies` as its first step, emits that array as a first-class
   output field, and sets `ensemble_fallback_used: true` only when the
   vendor array is empty for every candidate service. The ensemble skill
   refuses to run if upstream vendor anomalies existed.

4. **`cross-agent-validator`** — the consistency gate Claude Code doesn't
   have natively. Runs between every phase and refuses to let the pipeline
   continue with contradictions (e.g. signals about service X when intake
   routed to service Y; a fix PR that touches files outside the affected
   services).

Plus one tiny helper:

5. **`module-router`** — turns "checkout is broken" into
   `checkout-service`, using literal + synonym matching over
   `known_services`. Invoked by `intake` only when the ticket doesn't name
   a component. Flags `needs_disambiguation` instead of guessing.

## How the pieces wire together

```
.claude/
├── agents/
│   ├── orchestrator.md    # sonnet — owns the fixed phase sequence
│   ├── intake.md          # haiku  — Atlassian / ADO MCP
│   ├── signals.md         # haiku  — Datadog / Dynatrace / PagerDuty / GitHub MCP
│   ├── prior-incident.md  # haiku  — Confluence / ADO Wiki / PagerDuty MCP
│   ├── fix-and-test.md    # sonnet — GitHub MCP
│   └── validator.md       # haiku  — runs between every phase
│
├── skills/
│   ├── time-window-selector/   # the headline custom IP
│   ├── bm25-rerank/
│   ├── anomaly-ensemble/       # fallback only
│   ├── cross-agent-validator/
│   └── module-router/
│
└── commands/
    └── rca.md             # /rca <incident-id> — entry point
```

`.mcp.json` at repo root declares the six MCP servers. `CLAUDE.md` is loaded
automatically and gives Claude the project-wide conventions.

## The runtime story

1. A GitHub issue is opened with label `incident`, or an on-call manually
   runs `/rca INC-1234`.
2. `.github/workflows/rca.yml` fires, installs Claude Code CLI, runs it
   headless with `-p "/rca INC-1234"`.
3. `rca.md` command checks MCP availability, then delegates to the
   `orchestrator` sub-agent.
4. Orchestrator runs: `intake → time-window-selector → signals → prior-incident → fix-and-test`,
   with `validator` between every step.
5. Postmortem is posted back as a comment on the incident issue. Fix PR is
   opened and assigned to the reviewers in `RCA_PR_REVIEWERS`
   (default: `copilot`).

## What this buys vs pure Claude Code

Pure Claude Code with a few MCPs gives you the **what** — "here are some
anomalies, here is a possible fix". It does not give you:

- Defensible time-window selection from vague tickets.
- Cross-phase consistency checking (the gate that stops a bad hypothesis
  from becoming a bad PR).
- A deterministic, explainable reranker for prior incidents.

Those are the only places we ship custom code, and they are wrapped as
skills so they are first-class citizens in the Claude Code ecosystem — not
a parallel runtime.

## What this buys vs pure Datadog AIOps / Dynatrace Davis

AIOps tools are excellent inside their own telemetry. They do not know
about tickets, requirement docs, prior postmortems, code, or PRs. The
pipeline uses AIOps as a first-class signal provider (via MCP) and then
stitches its output to everything AIOps doesn't see.

Concretely, the **Watchdog-first contract** runs through three seams:

1. `signals` agent pulls `vendor_anomalies` from Datadog Watchdog and
   Dynatrace Davis **before any other observability query** and emits
   them as a structured output field (service, metric, severity, start,
   end, `insight_url`).
2. `time-window-selector` consumes that array as its **highest-weight
   prior** (left-skewed Gaussian, severity-scaled: CRITICAL 1.3, HIGH 1.0,
   MEDIUM 0.7, LOW 0.4; width scales with the anomaly span, capped 25 min
   so a multi-hour flag doesn't smear the posterior). Vendor hits are
   listed first in the rationale string.
3. `anomaly-ensemble` (our custom fallback) only runs when that array is
   empty for every candidate service; `ensemble_fallback_used: true` on
   signals output is the honesty flag in the final report.

This is the difference between "we also ran Watchdog" and "Watchdog is
the anchor of our window-selection posterior." Jothi was explicit on this
during review — "they have an AIOps module, honor it" — and the contract
above is what wires that instruction into the chassis, not just the docs.

## Regression & fixture tooling

Three utilities keep the chassis honest without needing live MCP
credentials:

- **`scripts/fixture_harness.py`** — runs every skill against
  `.claude/fixtures/INC-DEMO-001/` (canned MCP-shaped JSON) and asserts
  structural invariants: module-router picks `inventory-service`,
  time-window-selector pins 08:10–08:50 with change-point + deploy + page
  evidence, BM25 reranks `PM-42` to the top, and the cross-agent-validator
  correctly flags a contrived contradiction. A separate
  `time_window_selector_vendor` check re-runs the selector **with and
  without** `vendor_anomalies.json` fed in, and asserts that (a) the top
  window still contains the target timestamp, (b) Watchdog evidence
  surfaces in `supporting_evidence.vendor_anomalies`, and (c) confidence
  does not regress vs the no-vendor baseline — the regression gate that
  proves vendor AIOps is actually fused into the posterior, not just
  logged alongside. `make fixture`.
- **`scripts/verify_mcp_config.py`** — structurally validates `.mcp.json`
  (server types, required fields, `${VAR}` references). `make verify-mcp`
  for PR checks (warnings on missing env vars),
  `make verify-mcp-strict` for deploy checks (fails on missing env vars).
- **`scripts/seed_bug.py`** — reversibly seeds the off-by-one boundary bug
  in `python-inventory-service/app.py` so `fix-and-test` has a concrete
  failing test to target. `make seed-bug` / `make unseed-bug`. The
  corresponding `tests/test_reserve_boundary.py` fails 2/3 when seeded,
  passes 3/3 when reverted.

`make ci-chassis` runs all three end-to-end. Also wired into
`.github/workflows/fixture.yml` so every PR gets this regression gate.

## Operational notes

- **AnomalyDetector cold-start.** Training (250-epoch autoencoder + 100
  isolation trees) takes ~30 s. First invocation pays that cost; every
  subsequent one loads a pickle cache at
  `.claude/skills/anomaly-ensemble/cache/`. Set
  `RCA_SKIP_DETECTOR_CACHE=1` to force retrain.
- **CUSUM magnitude clamp.** Upstream `cusum.py` clamps std to `1e-10`
  when the baseline is flat, which makes `magnitude = (x-mu)/std` blow up
  to ~1e9. The time-window-selector wrapper now caps magnitude at 20σ in
  its output — presentation-only, the detection logic is unchanged.
- **IsolationForest randint fix.** Upstream
  `agents/algorithms/isolation_forest.py` was using
  `random.Random.randint(0, n_features)` to pick a split feature, which
  is inclusive on both ends and would produce `feat == n_features` —
  raising `IndexError: index n out of bounds for axis 1 with size n` on
  the next line. Fixed to `randrange(n_features)` (half-open, numpy
  convention) and `anomaly_ensemble` is now a fatal fixture check.

## Open questions for review

- **MCP maturity.** Atlassian Rovo MCP is GA (Feb 2026), Datadog MCP is GA
  (March 2026), Dynatrace and PagerDuty MCPs are stable. ADO MCP is still
  local-only; remote MCP for ADO is the next thing to watch.
- **Self-host vs hosted.** For VPC-only deployments, Datadog MCP swaps to
  `winor30/mcp-server-datadog`; Atlassian currently requires proxying
  Rovo. `.mcp.json` has comments for each.
- **Cost.** Model tiering pushes the per-incident cost down — collectors
  on haiku, orchestration + fixes on sonnet. The validator is haiku and
  runs N+1 times, so it needs to stay cheap by design.

## Where to go next

1. Provision staging MCP credentials and run `/rca INC-DEMO-001` end-to-end
   against live Atlassian / Datadog / GitHub staging tenants (the one
   thing the offline fixture harness cannot cover).
2. Benchmark time-window-selector on 20–30 replayed real incidents vs
   "naive: 30 min around the page".
3. Decide whether `fix-and-test` stays at sonnet or goes to opus for
   safety-critical services.

## The shakedown — what stands between chassis-green and pilot-green

`make ci-chassis` exercises the skill scripts against deterministic
fixtures. `make shakedown` exercises the full agent topology with
`RCA_DRY_RUN=1` — every MCP call is substituted with fixture data, no
PR opens. Both are offline.

Neither is a substitute for running the pipeline end-to-end against a
real incident with real MCP credentials, with a platform engineer
watching live. **That manual shakedown run is a prerequisite for
enabling the automation-on-label trigger**, no matter how green the
offline suite is. See `RUNBOOK.md` → "Pilot shakedown" for the full
procedure and the pre-rollout checklist.
