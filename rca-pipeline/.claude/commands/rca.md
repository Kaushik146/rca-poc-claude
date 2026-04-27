---
description: Run the RCA pipeline end-to-end on an incident ticket. Usage - /rca INC-1234
argument-hint: <incident-id>
allowed-tools: Task, Read, Bash
---

# /rca — Root Cause Analysis

Run the full RCA pipeline on the incident identified by $ARGUMENTS.

## Pre-flight

- **Kill switch.** If the environment variable `RCA_DISABLED` is set to `1`
  (or `true`), stop immediately, print `RCA pipeline is disabled via
  RCA_DISABLED`, and exit cleanly without making any MCP call. This is the
  emergency-off for operators when the pipeline is misbehaving.
- **Dry-run mode.** If `RCA_DRY_RUN` is set to `1` (or `true`), the
  pipeline runs the full agent topology but substitutes fixture data
  from `.claude/fixtures/<incident_id>/` (or `INC-DEMO-001` if the
  per-incident fixture is missing) for every MCP call, and `fix-and-test`
  refuses to open a PR. The purpose is to exercise chassis wiring end-to-
  end without live credentials before a pilot. The shakedown procedure in
  `RUNBOOK.md` expects this mode.
- If $ARGUMENTS is empty, stop and ask the user for an incident ID. Do not proceed.
- Verify the active MCP servers include at least one ticketing source (Atlassian or ADO) and one observability source (Datadog or Dynatrace). If either category is missing, warn and stop. In dry-run mode, skip this check and log `dry_run_mcp_check_skipped: true` in the final report.
- **Staged execution.** Read `RCA_STAGE` from the environment:
  - Unset / `full` — run all phases. This is the default.
  - `propose-only` — run phases 1–3, invoke `fix-and-test` in propose-only
    mode (generates the fix and runs tests but posts the proposal as a
    comment instead of opening a PR). This is the production default when
    `RCA_REQUIRE_FIX_APPROVAL=1` is set (see below).
  - `open-pr` — skip phases 1–3, read a prior proposal from
    `.rca/fix-proposals/<incident_id>.json`, and open the PR. This is the
    stage triggered by a human applying the `rca:approve-fix` label to the
    incident issue. Refuse to run if no proposal file exists.
- **Human-in-the-loop default.** If `RCA_REQUIRE_FIX_APPROVAL` is unset or
  set to `1`/`true`, treat an unset `RCA_STAGE` as `propose-only`. The
  fix-and-test agent will post the proposed diff + test results as a
  comment and stop; no PR opens until a human applies the approval label
  and the workflow re-triggers with `RCA_STAGE=open-pr`. Set
  `RCA_REQUIRE_FIX_APPROVAL=0` only in non-production environments where
  auto-PR is acceptable.

## Execution

Invoke the `orchestrator` sub-agent via the `Task` tool with:

```
incident_id: $ARGUMENTS
```

The orchestrator runs the fixed 4-phase sequence:

1. `intake` — pull ticket + requirement
2. Invoke the `time-window-selector` skill
3. `signals` — pull logs/traces/metrics/deploys/pages for the selected window
4. `prior-incident` — search past postmortems and rerank
5. `fix-and-test` — generate patch, run tests, open PR
6. Between every phase, the `validator` sub-agent runs with the `cross-agent-validator` skill

## Post-execution

Print a compact human-readable summary:
- Ticket title + ID
- Chosen time window + confidence
- Top root-cause hypothesis
- Top prior-incident match (if any)
- PR URL
- Any blocking warnings from the validator

Link the full JSON report at `.rca/reports/<ticket_id>-<timestamp>.json` for downstream GitHub Action posting.

## Non-negotiables

- Never merge the PR. Reviewer assignment only.
- Never skip the validator between phases.
- Never ignore `RCA_DISABLED` or `RCA_REQUIRE_FIX_APPROVAL`. Bypassing a
  kill switch or the HITL gate is a production-severity mistake, not an
  optimization.
- If the `time-window-selector` returns confidence < 0.4, stop and ask the user for an approximate time. Do not continue on a guessed window.
