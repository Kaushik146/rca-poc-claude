# RUNBOOK.md — Operating the RCA pipeline

This is the on-call runbook for the RCA pipeline itself, not for the
incidents it investigates. Keep it open when something looks wrong with
pipeline behavior, when a dependency is degraded, or when you need to
stop the pipeline from doing anything while you figure out why.

## Who owns what

| Thing                              | Owner                          |
| ---------------------------------- | ------------------------------ |
| Pipeline health + `RCA_DISABLED`   | Platform Engineering on-call   |
| MCP credential rotation            | Platform Engineering + SecOps  |
| Review of auto-posted fix PRs      | Service CODEOWNERS + `@copilot` |
| Escalation for false-positive fixes| Platform Engineering lead      |
| Anthropic DPA + data questions     | Procurement + Legal            |

Fill in the names/teams for your org before rollout. If any row is empty
at go-live, the pipeline is not ready for go-live.

## Kill switch: how to stop the pipeline

Single-button stop, fastest method:

1. Go to the repo's **Settings → Secrets and variables → Actions**.
2. Open the **Variables** tab.
3. Set (or create) `RCA_DISABLED=1`.
4. Any subsequent trigger — new incident issue, label, or manual
   dispatch — will exit cleanly with a warning message. No MCP calls,
   no Claude API calls, no writes anywhere.

Second-line: disable the workflow itself.

1. Repo **Actions → "RCA on incident label"** → top-right `…` menu →
   **Disable workflow**.
2. Use this if `RCA_DISABLED` is somehow not being respected (shouldn't
   happen, but the two-layer defense is the point).

Third-line: revoke credentials.

1. If the pipeline is doing something actively harmful (e.g. opening
   rogue PRs), revoke `GITHUB_PAT` *first* — that cuts its write path.
2. Then revoke the other credentials to stop observability reads.
3. Rotate all credentials before re-enabling; the guarantee the revoked
   ones are not in use anymore.

## Common failure modes and what to do

### "Pipeline runs but produces no time window"

Symptom: `/rca` reports `time_window_selector returned confidence < 0.4`
and stops.

This is correct behavior, not a bug. The pipeline refuses to guess a
window. Resolution: the user who opened the incident ticket adds a
time hint to the ticket body ("around 8:30 AM UTC on April 18th") and
re-triggers with `workflow_dispatch`. Do not lower the 0.4 threshold —
that's the whole point of the gate.

### "Watchdog/Davis returned nothing; pipeline fell back to ensemble"

Symptom: the final report's `execution_meta` shows
`ensemble_fallback_used: true`.

Not an outage. It means vendor AIOps had no flagged anomaly for the
candidate services in the window. Before production, this would be rare
(~5% of incidents); in early pilot, it may be common until Datadog's
Watchdog training data accumulates for the customer's services. The
report's hypothesis is still valid, just derived from the fallback
ensemble. Flag for review if you see it more than ~20% of incidents in
a week — might indicate a Datadog configuration gap on the customer
side (no APM agent on the service, no error-rate metric published).

### "Validator blocks a phase with a contradiction"

Symptom: pipeline stops mid-run, postmortem comment reads "blocking
warnings: contradiction — fix touches files in service X but signals
covered service Y."

Correct behavior. Either the intake phase mis-routed, or the
fix-and-test agent hallucinated a fix outside the investigated scope.
Investigate:
1. Read `.rca/reports/<id>.json` — inspect the intake output's
   `affected_components` and the fix-and-test output's `files_changed`.
2. If intake was wrong: fix the ticket (add the correct component) and
   re-trigger.
3. If the fix was wrong: this is a Claude-reasoning issue, not a
   pipeline issue. Leave a detailed `thumbs-down` feedback on the
   postmortem comment and do not re-trigger until a human reviews the
   proposed diff.

### "MCP server timeout / 429 / 5xx"

Symptom: workflow step fails with a stderr mentioning a specific MCP
server.

Remediation order:
1. **Which server?** Look at the stream-json log in the uploaded
   artifact. Grep for the server name.
2. **Upstream status.** Check the vendor status page:
   - Atlassian: https://status.atlassian.com
   - Datadog: https://status.datadoghq.com
   - Dynatrace: https://status.dynatrace.com
   - GitHub: https://www.githubstatus.com
   - PagerDuty: https://status.pagerduty.com
3. **Rate limit (429).** Every MCP server has per-tenant quotas. If
   multiple incidents fire in a short window, rate limits can bite.
   Retry after the window; if chronic, raise with the vendor or stagger
   triggers via a workflow concurrency key.
4. **Auth failure (401/403).** Most common cause of a fresh-deploy
   break. Run `make verify-mcp-strict` locally with the production env
   vars exported (or in an ops jumphost) to confirm the creds are set
   and scoped. If the creds are set, check scope against `SECURITY.md` —
   the most common gotcha is a Datadog app key on a service account
   that lost its role, or an Atlassian OAuth token that rotated without
   updating the repo secret.

Partial-failure decision tree:

| What's down                    | Can the pipeline run?                                |
| ------------------------------ | ---------------------------------------------------- |
| Datadog only                   | Yes, if Dynatrace is configured (they are interchangeable for AIOps). Otherwise no — primary anomaly signal is missing. |
| Dynatrace only                 | Yes, if Datadog is configured. Same logic.           |
| GitHub                         | Analysis phases run; fix-and-test fails.             |
| Atlassian **and** Azure DevOps | No. Ticketing is required for intake.                |
| Atlassian only (ADO available) | Yes, for ADO-first customers.                        |
| PagerDuty                      | Degraded — `pages` prior is absent from window       |
|                                | selection; pipeline emits `pages_missing: true` and  |
|                                | lowers confidence ceiling to 0.8.                    |
| Anthropic API                  | No — Claude Code cannot run headless without it.     |

### "Propose-only comment was posted but no label ever got applied"

Symptom: the fix proposal comment is sitting on an incident issue for
hours/days with no `rca:approve-fix` or `rca:reject`.

This is the HITL gate doing its job. The pipeline is waiting on a human
decision; it is *not* a pipeline bug. Options:
1. Contact the service owner (CODEOWNERS of the affected paths) and ask
   them to review the proposal.
2. If the incident is stale and nobody will approve, have the
   responsible engineer apply `rca:reject` to close the loop cleanly.
   This expires the proposal artifact.
3. Do **not** set `RCA_REQUIRE_FIX_APPROVAL=0` to "unblock" it. That
   disables HITL for every future incident, not just this one.

### "Workflow fires twice for the same incident"

Common cause: the incident issue had `incident` applied at creation and
then re-applied (or another label added), and GitHub fires
`issues.opened` and `issues.labeled` both. This is a GitHub behavior,
not a pipeline bug.

Mitigation already in place: the workflow's `if:` clause filters on
specific label names. If you see duplicate runs:
1. Check the workflow concurrency setting — add a
   `concurrency: rca-<issue-number>` group to serialize.
2. Check for a label-reapply loop from another automation.

### "Cost per incident is higher than expected"

Symptom: monthly Anthropic bill is climbing faster than incident count.

Investigate, in order:
1. **Unexpected re-triggers.** Count `workflow_dispatch` + label events.
   If each incident is running 5× due to label thrash, that's 5× cost.
2. **Context expansion.** Sonnet-tier orchestrator + long requirement
   docs + long postmortems = big context. Check the stream-json log's
   token counts in the RCA artifact.
3. **Model-tier drift.** Confirm haiku is still configured in the
   collector agents (`intake`, `signals`, `prior-incident`, `validator`).
   A misconfigured model: frontmatter that upgrades a collector to
   sonnet will double the bill quietly.

### "Cold-start IsolationForest trains slowly on first run"

Symptom: first `/rca` after a deploy takes ~30 seconds longer than
subsequent runs.

Expected. The ensemble detector trains once per deploy and caches at
`.claude/skills/anomaly-ensemble/cache/`. Subsequent runs load the
pickle in milliseconds. Force-retrain with
`RCA_SKIP_DETECTOR_CACHE=1` — only when intentionally regenerating.

## Escalation path

Severity ladder (tune to your org's matrix):

1. **P4 — Runbook-solvable.** A stuck incident, a known-failing MCP, a
   cost anomaly. On-call handles without waking anyone.
2. **P3 — Needs platform-eng.** A new validator contradiction pattern, a
   repeatable false-positive, a wider-than-expected Watchdog fallback
   rate. File a ticket, continue through business hours.
3. **P2 — Pipeline actively wrong.** Fix PRs being opened that touch
   the wrong files, proposals posted to wrong tickets, data being
   written somewhere it shouldn't be. Hit the kill switch first, then
   page platform-eng lead.
4. **P1 — Pipeline doing something harmful.** Exfiltration, merging PRs,
   deleting data. Revoke `GITHUB_PAT` first, kill switch second, page
   SecOps + platform lead.

If you're unsure between P2 and P3, treat it as P2. The pipeline can
remain off while you investigate; it cannot be un-pushed.

## Disable the pipeline for one repo only

If the pipeline is misbehaving on a single service but should keep
running elsewhere:

1. Remove the `incident` label option from the affected repo's label
   set, or
2. Add the affected repo to a per-repo denylist (next version — not
   shipped yet; log a follow-up if needed), or
3. Disable the workflow file in just that repo via Actions UI.

Don't rely on "I told the service owners not to apply the label" — labels
get applied by templates, automation, and muscle memory. Use a config
gate.

## Pilot shakedown — mandatory before any automation fires

"Chassis CI green" and "dry-run green" mean the *topology* is wired and
the *skill scripts* work offline. Neither of those tests the full
system against a real incident. Before any customer's incident label
triggers an automated `/rca`, a platform-engineer MUST:

1. Run `make shakedown` locally. This exercises the agent topology
   against `INC-DEMO-001` with `RCA_DRY_RUN=1` — every MCP call is
   substituted with fixture data, no PR opens, no comment posts. It
   proves the end-to-end agent graph runs without blowing up.
2. Workflow-dispatch `/rca` once against a **real historical incident**
   (pick one with a known root cause, already resolved) in
   `propose-only` mode, with `RCA_DRY_RUN=false`, with real MCP creds.
   A platform-eng engineer watches the run live. Review:
   - Did intake fetch the right ticket?
   - Did time-window-selector pick a sane window? What confidence?
   - Did signals pull from Watchdog/Davis first?
   - Did the proposal comment land on the incident issue with the
     hidden JSON payload intact?
   - Could the open-pr stage hydrate the proposal from the comment?
3. Run the open-pr stage — apply `rca:approve-fix` to the test issue
   and watch the hydrate step succeed, `diff_sha256` verify, tests
   re-run, and the PR open with the correct reviewer(s). Close the PR
   without merging.

Only after steps 1–3 pass cleanly does the pipeline qualify as "pilot
ready." Skipping the shakedown has been the single most common cause of
"our LLM ops tool exploded on first real use" in other teams' postmortems
— do not skip it.

## Pre-rollout checklist

Copy this into your go-live ticket and don't tick green until every box
is real:

- [ ] `RCA_DISABLED` GitHub repo variable exists (set to `0`).
- [ ] `RCA_REQUIRE_FIX_APPROVAL` GitHub repo variable exists (set to `1`).
- [ ] `RCA_PR_REVIEWERS` GitHub repo variable set (default `copilot` if
      unset is fine; override only if org uses a different review bot).
- [ ] All MCP secrets loaded and `make verify-mcp-strict` passes.
- [ ] `make ci-chassis` green.
- [ ] `make shakedown` green (offline dry-run).
- [ ] Manual shakedown run executed against a real historical incident
      in `propose-only` mode with platform-eng watching live.
- [ ] `open-pr` stage exercised against the shakedown's proposal
      comment — hydration + SHA verification + test re-run succeed.
- [ ] `SECURITY.md` reviewed by SecOps; every credential at minimum scope.
- [ ] Branch protection live on every repo the pipeline may touch.
- [ ] `CODEOWNERS` on critical paths includes a real human team.
- [ ] `rca:approve-fix` and `rca:reject` labels created in every repo.
- [ ] On-call rotation has this runbook bookmarked.
- [ ] First real-incident pilot scoped to one friendly team, with a
      platform-eng engineer watching the first 3 runs live.

## Post-incident review of the pipeline itself

Any time the pipeline does something wrong, run a proper post-incident
review on *it*:

1. Link the original incident, the pipeline's postmortem, and the
   problem behavior.
2. Identify which phase went wrong (intake, time-window, signals,
   prior-incident, fix-and-test, validator).
3. Ask: did any guardrail catch it? If not, what guardrail should
   have? Add it to `cross-agent-validator` or a skill's guardrail list.
4. Add a fixture reproducing the failure to
   `.claude/fixtures/<new-id>/` and a harness check that would have
   caught it. Don't let the same failure class happen twice.

## Reference

- Pipeline architecture: `README_CLAUDE_CODE.md`
- Skill contracts: `.claude/skills/*/SKILL.md`
- Agent contracts: `.claude/agents/*.md`
- Fixture harness: `scripts/fixture_harness.py`
- MCP credential scopes: `SECURITY.md`
- Orchestration decisions: `CLAUDE.md`
