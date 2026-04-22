# SECURITY.md — Least-privilege MCP credentials for the RCA pipeline

This document enumerates every secret the pipeline needs, the minimum scope
that credential should hold, and what it must **not** be allowed to do. The
default posture is read-mostly: the pipeline reads enterprise observability
and ticketing data, synthesizes an RCA report, and opens (never merges) a
pull request. Credentials should be issued accordingly.

It also covers rotation, network egress for allowlisting, and the
data-handling / DPA note that most enterprise procurement teams will ask
about before any secret gets provisioned.

## Threat model in one paragraph

The pipeline runs headless in CI (GitHub Actions) with access to six MCP
servers. If any one of those credentials is stolen or the pipeline is
tricked into misbehaving (prompt injection via ticket body, compromised
MCP server, supply-chain on a dependency) the blast radius is bounded by
the scope of the credential. That is the entire point of this document:
no credential should have a blast radius larger than "read some data and
open one PR that a human reviewer has to approve."

## Per-credential scope

### `ATLASSIAN_OAUTH_TOKEN` (Atlassian Rovo / Jira + Confluence)

What the pipeline does with it:
- Reads the incident ticket (Jira) by key.
- Reads the affected service's requirement doc (Confluence).
- Reads past postmortems (Confluence).
- Posts a single comment with the RCA summary back on the ticket.

Minimum OAuth 3LO scopes:
- `read:jira-work` — read issues.
- `read:jira-user` — resolve assignees in the RCA context.
- `write:comment:jira` — post the single RCA comment back. Nothing broader.
- `read:confluence-content.all` — read postmortems and requirement docs.

Forbidden scopes:
- `write:jira-work` — the pipeline must never transition, close, reassign,
  or modify a ticket beyond its one summary comment.
- `manage:jira-configuration`, `manage:jira-project` — out of scope.
- Any `delete:*` scope — non-negotiable.
- `write:confluence-content` — the pipeline reads, it does not author.

Rotation: 90 days for production, 30 days during pilot.

### `AZURE_DEVOPS_ORG_URL` + `AZURE_DEVOPS_PAT`

What the pipeline does with it:
- Reads work items (tickets) and wiki pages (postmortems) when the customer
  uses ADO instead of Atlassian.
- Reads repository contents for the `fix-and-test` agent to propose a diff.
- Opens a pull request. Never merges.
- Adds `@copilot` as reviewer and posts a comment.

Minimum PAT scopes:
- `vso.work` (Work Items: Read).
- `vso.wiki` (Wiki: Read).
- `vso.code` (Code: Read) plus `vso.code_write` scoped **only** to the
  specific repositories the pipeline may touch. ADO PATs are not per-repo
  scoped natively; compensate with branch policies that block anything
  outside `rca/*` branches.

Forbidden scopes:
- `vso.code_manage` — allows force-push and branch deletion.
- `vso.build`, `vso.release`, `vso.packaging_manage` — pipeline never
  controls builds, releases, or feeds.
- `vso.identity`, `vso.project_manage`, `vso.tokens` — out of scope.

Compensating controls:
- Branch policy on `main`/`master` requiring 2 reviewers, preventing
  force-push, and requiring a linked work item.
- Repository-level policy rejecting merges from `rca/*` branches unless
  the linked PR carries an approved review.

Rotation: 90 days. ADO PATs do not auto-rotate — set a calendar reminder
and pair with a monitoring check that alerts at 14 days before expiry.

### `DD_API_KEY` + `DD_APP_KEY` (Datadog)

What the pipeline does with it:
- Reads logs, traces, metrics, service definitions for the candidate
  services inside the chosen time window.
- Reads Watchdog Insights.
- Reads Datadog Incident Management entries if the customer uses it.

Minimum Datadog role permissions (create a dedicated role, assign the
application key to a service account with only this role):
- `logs_read_data`
- `logs_read_index_data`
- `metrics_read`
- `apm_read`
- `synthetics_read` (only if synthetics-based anomaly surfacing is in use)
- `incident_read`
- `monitors_read`
- `watchdog_read`

Forbidden permissions:
- `metrics_write`, `logs_write_*`, `monitors_write`, `monitors_downtime`,
  `incidents_write`, `dashboards_write`, `service_account_write`,
  `synthetics_write`, `org_management`, `billing_*`. The pipeline must
  **never** silence a monitor, modify a dashboard, or create/resolve an
  incident in Datadog.

Key handling:
- The API key is the org-wide one; it is less sensitive than the app key.
- The **app key is the identity**. Create it against a service-account
  user with the locked-down role above — not a human user, never a
  privileged user. If the app key leaks, rotating it is the same motion
  as disabling that service account.

Rotation: 90 days for app keys. Datadog does not force rotation; set up a
calendar alert. Rotate immediately on staff change for anyone who had
access to the app key.

### `DT_ENV_URL` + `DT_API_TOKEN` (Dynatrace)

What the pipeline does with it:
- Reads DQL queries over Grail (logs, metrics, events).
- Reads active Davis AI problems and anomaly events.
- Reads entity and deployment metadata.

Minimum Dynatrace API token scopes:
- `metrics.read`
- `entities.read`
- `logs.read`
- `events.read`
- `problems.read`
- `davis.data.read`
- `openpipeline.events.read` (only if Grail OpenPipeline is used)

Forbidden scopes:
- `metrics.write`, `problems.write`, `deployment.write`, `entities.write`,
  `events.ingest`, `settings.write`, `credentialVault.write`,
  `oneAgents.write`. Same principle as Datadog: the pipeline observes, it
  does not act on the observability stack.

Rotation: 90 days. Dynatrace supports token expiration dates — always set
one. A token with no expiration is a bug.

### `GITHUB_PAT` (GitHub fine-grained PAT, not classic)

What the pipeline does with it:
- Reads repository contents for the services listed in the fixture.
- Reads pull request metadata and commit history.
- Opens a pull request (branch `rca/<incident-id>`) with the proposed fix.
- Adds `@copilot` as reviewer.
- Reads and writes issue comments on the triggering incident issue.
- Lists recent deploys (merged PRs, commit history to main) for the
  time-window-selector's deploy prior.

Minimum fine-grained PAT permissions, scoped to **specific repositories
only** (not "all repositories"):
- **Contents**: Read (reads source, reads file trees).
- **Contents**: Write is required on **only** the service repos the
  pipeline may fix — ideally gate this behind a separate PAT so the
  read-only PAT can be used for everything except the fix-and-test
  agent's write path.
- **Pull requests**: Read + Write (opens PRs, adds reviewers, posts
  descriptions).
- **Issues**: Read + Write (reads the incident issue, posts the RCA
  summary comment).
- **Metadata**: Read (repository listing; this is mandatory on every PAT).
- **Commit statuses**: Read (for checking CI status before declaring a
  fix "verified").

Forbidden permissions:
- **Administration**, **Secrets**, **Variables**, **Actions** (Write),
  **Workflows**, **Security events**, **Packages**, **Deployments**,
  **Environments**, **Branches/settings**. Non-negotiable.
- "All repositories" scope — always a finite allowlist.

Compensating controls:
- Branch protection on `main` / `master` requiring at least one human
  reviewer, linear history, and passing CI. Even with write access to
  Contents, the PAT cannot merge to a protected branch.
- `CODEOWNERS` on critical paths so a real owner is always on the
  reviewer list — `@copilot` is a second opinion, not a single point of
  approval.
- Repository ruleset denying force-push, `--no-verify`, and tag deletion.

Rotation: fine-grained PATs support expiration — set 90 days or less.
Rotate immediately on any change to the CI service account.

### `PAGERDUTY_API_TOKEN`

What the pipeline does with it:
- Reads active and recent incidents.
- Reads page history for the candidate services in the window.
- Reads service and team mappings to resolve "who paged whom."

Minimum scope:
- **Read-Only API Key** (this is a distinct key type in PagerDuty, not a
  scoped subset of a General Access Key — use it, not a general key).

Forbidden:
- **General Access Key** or **User API Key** with full access. The
  pipeline must not be able to trigger, acknowledge, resolve, snooze, or
  reassign pages. Not even in theory. Create-incident scope in
  particular is a full-severity operational hazard; it must not exist on
  this credential.

Rotation: 90 days. PagerDuty Read-Only keys do not auto-rotate.

## Secret storage

- Secrets live in GitHub Actions repo secrets or the enterprise's
  existing secret manager (HashiCorp Vault, AWS Secrets Manager, Azure
  Key Vault). They are injected into the CI job via environment
  variables; `.mcp.json` uses `${VAR}` placeholders and is checked into
  the repo **without** any real values.
- `scripts/verify_mcp_config.py --strict` is the pre-deploy gate: it
  refuses to run if any `${VAR}` reference is unset. Wire it into the
  deploy workflow, not just PR checks.
- Local development: use a `.env` file that is `.gitignore`'d, and a
  direnv-style loader. Never commit `.env`.

## Rotation policy

| Credential              | Rotation  | Trigger |
| ----------------------- | --------- | --------------------------------- |
| `ATLASSIAN_OAUTH_TOKEN` | 90 days   | Staff departure, suspected leak   |
| `AZURE_DEVOPS_PAT`      | 90 days   | Expiry alert at T-14 days         |
| `DD_API_KEY` / `DD_APP_KEY` | 90 days | Service-account ownership change |
| `DT_API_TOKEN`          | 90 days   | Token lifetime set at creation    |
| `GITHUB_PAT`            | 90 days   | PAT expires; CI build fails clean |
| `PAGERDUTY_API_TOKEN`   | 90 days   | Staff departure                   |

Immediate-rotation triggers (all credentials):
- Departure of anyone with access to the secret manager.
- Any indication of repo-secret exposure (force-pushed commit,
  accidental log, dependency leak).
- CI image compromise.

## Network egress

Allowlist these outbound destinations for the CI runner that executes
`/rca`. No inbound connections are required; the pipeline is strictly
outbound.

- `mcp.atlassian.com` (HTTPS)
- `api.atlassian.com` (HTTPS, OAuth refresh)
- `mcp.datadoghq.com` (HTTPS) — or `mcp.datadoghq.eu` for EU tenants.
- `<your-env>.live.dynatrace.com` (HTTPS) — per-tenant URL, parameterize.
- `api.github.com`, `ghcr.io` (HTTPS, for the Docker-based GitHub MCP).
- `api.pagerduty.com` (HTTPS).
- Your Azure DevOps organization URL (HTTPS).
- `api.anthropic.com` (HTTPS) — Claude Code's own outbound.

Explicit denies:
- No outbound to arbitrary hosts. If the CI runner has broad internet
  access, the pipeline is fine; if it's in a VPC, these seven are the
  allowlist.

## Data handling / DPA

The RCA pipeline flows incident tickets, requirement docs, logs, and
postmortems through the Claude API. For most enterprises this requires
a Data Processing Agreement with Anthropic covering the specific data
categories in transit:
- Incident ticket text (possibly PII of reporters, customer identifiers).
- Requirement docs (business logic, possibly competitively sensitive).
- Logs (possibly PII if logs are not scrubbed upstream).
- Postmortems (may reference individuals, customer names).

Action items before production:
- Confirm an Anthropic DPA is in place; Anthropic offers zero-data-
  retention on the API for business customers.
- Audit log content upstream — if logs contain raw PII (user emails,
  payment data) they should be scrubbed in Datadog / Dynatrace before
  they reach this pipeline, not after.
- For regulated workloads (HIPAA, PCI, FINRA), the enterprise buyer
  needs the equivalent compliance attestation; this is a procurement
  conversation, not a code conversation.

## Prompt-injection & MCP-server trust

Two additional attack surfaces:

1. **Prompt injection via ticket body.** A malicious actor with ticket-
   create access could embed instructions in the ticket intended to make
   the pipeline open a PR with a malicious payload. Mitigations already
   wired in:
   - `fix-and-test` PRs are never merged automatically.
   - `cross-agent-validator` refuses to let the pipeline continue if the
     fix touches files outside the affected services.
   - `@copilot` reviewer and `CODEOWNERS` require a real human approval.
   Gap: no sandboxing of the ticket-text prior in the time-window
   selector. A crafted ticket could bias the window. Follow-up: sanitize
   ticket-text timestamps to a structured field rather than free-form.

2. **Compromised MCP server.** If any of the six MCP servers is
   compromised (supply chain, rogue update), it can feed arbitrary data
   back to the pipeline. Mitigations:
   - Pin MCP server versions in `.mcp.json` — today they are unpinned
     (`@microsoft/azure-devops-mcp-server`, `@pagerduty/mcp-server`,
     `ghcr.io/github/github-mcp-server`). Pin to a verified SHA or
     version tag before production.
   - Re-audit on every MCP version bump.
   - Treat MCP output as untrusted input: the validator's contradiction
     checks already catch the most likely pathological outputs (fix
     touches files outside signals' scope), but the principle is "MCP is
     a remote service, not a local library."

## Quick checklist before provisioning production credentials

- [ ] Dedicated service accounts for every credential (no human users).
- [ ] Every credential scoped to the minimums above.
- [ ] Every credential has an expiration set at creation.
- [ ] Secrets stored in the enterprise secret manager, not CI secrets
      directly, if compliance requires central audit.
- [ ] `scripts/verify_mcp_config.py --strict` wired into the deploy
      workflow.
- [ ] Branch protection on every repo the pipeline may touch.
- [ ] `CODEOWNERS` on critical paths includes a real human team.
- [ ] MCP server versions pinned in `.mcp.json`.
- [ ] Egress allowlist configured on the CI runner (if VPC-gated).
- [ ] DPA with Anthropic in place for the data categories above.
- [ ] Runbook (see `RUNBOOK.md`) reviewed by on-call and escalation.
