# MCP Tool Audit

The RCA agents declare MCP tool access via wildcard in their frontmatter
(e.g. `tools: ..., mcp__datadog__*`). This document spells out what each
wildcard actually resolves to at runtime, what the agent's prose assumes
those tools can do, and where the live-creds shakedown must verify the
assumptions hold.

Status legend:

| Status | Meaning |
| ------ | ------- |
| ✅ verified | Tool name was seen in a real CLI init block against a live server |
| 🟡 expected | Tool name is what the published MCP docs say, but we have not yet seen it in a live init block from this repo |
| ❓ unknown | The agent assumes a capability exists but we haven't confirmed the tool name |

Every row marked 🟡 or ❓ is a shakedown priority — the platform engineer
doing the manual run should watch for the specific tool name and
re-classify the row.

## atlassian (Rovo MCP — Jira + Confluence)

**Connection status in sandbox:** connected.

**Tools observed in live init block:**

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__atlassian__getTeamworkGraphContext` | ✅ verified | `intake`, `prior-incident` |
| `mcp__atlassian__getTeamworkGraphObject` | ✅ verified | `intake`, `prior-incident` |

**Agent-prose expectations that these two tools must cover:**

- **Intake:** fetch a Jira issue by key (title, description, reporter,
  labels, linked issues, custom fields). Fetch a linked Confluence page
  by URL or ID.
- **Prior-incident:** search Confluence pages by query string across
  postmortem / RCA / incident-review spaces; return page content + URL
  + last-modified timestamp.

**Shakedown concern:** Rovo's teamwork-graph abstraction is a single
entry point that covers *both* Jira and Confluence. Agents written
against older, typed MCPs (e.g. `get_jira_issue`, `search_confluence`)
would expect separate tools. If the shakedown finds the agent
hesitating on "which tool do I call?", the fix is in the agent prose
(tell it explicitly: "all Atlassian reads go through
`getTeamworkGraphContext` or `getTeamworkGraphObject`"), not in the
wildcard.

## datadog (Datadog Remote MCP — GA March 2026)

**Connection status in sandbox:** not connected (no creds).

**Tools expected from docs** (toolsets: APM, logs, metrics, incidents,
Watchdog):

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__datadog__query-metrics` | 🟡 expected | `signals` (p99, error rate, CPU, memory) |
| `mcp__datadog__list-logs` / `query-logs` | 🟡 expected | `signals` (error-level logs per service) |
| `mcp__datadog__list-traces` | 🟡 expected | `signals` (traces with error=true) |
| `mcp__datadog__list-watchdog-insights` | 🟡 expected | `signals` step 1 (vendor AIOps first!) |
| `mcp__datadog__list-incidents` | 🟡 expected | `prior-incident` (optional, if org uses DD incidents) |

**Agent-prose expectations:**

- `signals` pulls Watchdog Insights *before* any other Datadog query.
  The exact tool name for the Watchdog-Insights endpoint is the single
  most important verification in the Datadog row — it's what the whole
  Watchdog-first contract rests on.
- Query shape: metric name, tag filter (`service:inventory-service`),
  time range (from `time_window.start` / `end`).

**Shakedown concern:** Datadog's MCP exposes different toolsets
depending on the account plan (APM tier includes traces; logs tier
includes logs). If the customer's plan doesn't include Watchdog, the
tool list will be missing `list-watchdog-insights` at init time. That's
an honest configuration answer — the `anomaly-ensemble` fallback is
designed for exactly that case — but the agent needs to recognize "tool
not present" vs "tool present but returned empty" (different code
paths: `ensemble_fallback_used: true` only for the second).

## dynatrace (Dynatrace MCP — stable)

**Connection status in sandbox:** not connected.

**Tools expected from docs** (exposes Davis AI + DQL over Grail):

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__dynatrace__list-problems` | 🟡 expected | `signals` step 1 (Davis anomalies) |
| `mcp__dynatrace__get-problem` | 🟡 expected | `signals` (drill into specific problem) |
| `mcp__dynatrace__query-dql` | 🟡 expected | `signals` (raw DQL over logs/metrics) |
| `mcp__dynatrace__list-entities` | 🟡 expected | `signals` (resolve service names to entity IDs) |

**Agent-prose expectations:** `signals` lists active Davis problems
overlapping the time window for the candidate services. Like the
Watchdog row, the Davis-problems endpoint is the anchor of the
Watchdog-first contract when Dynatrace is the primary observability
stack.

**Shakedown concern:** Davis Problems are scoped to entity IDs, not
service names. If the agent passes a service name (e.g.
"inventory-service") instead of the entity ID from `list-entities`,
the query returns empty. The agent prose should have a "resolve
service name to entity ID first" step. Today it doesn't — flag this
as a known gap if the Dynatrace row surfaces first.

## github (GitHub official MCP — local Docker + PAT)

**Connection status in sandbox:** not connected.

**Tools expected from docs:**

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__github__list_commits` | 🟡 expected | `signals` step 5 (deploys ±2h) |
| `mcp__github__list_pull_requests` | 🟡 expected | `signals` step 5 |
| `mcp__github__get_pull_request` | 🟡 expected | `signals` (drill into PR for files_changed) |
| `mcp__github__create_pull_request` | 🟡 expected | `fix-and-test` Flow B step 5 |
| `mcp__github__create_issue_comment` | 🟡 expected | `fix-and-test` Flow A step 5b (post proposal comment) |
| `mcp__github__get_issue` / `list_issue_comments` | 🟡 expected | workflow hydrator (live mode only) |
| `mcp__github__add_labels` | 🟡 expected | not used directly by agents — the `rca:approve-fix` label is applied by a human |
| `mcp__github__request_reviewers` | 🟡 expected | `fix-and-test` Flow B step 5 (assign `RCA_PR_REVIEWERS`) |

**Agent-prose expectations:**

- `fix-and-test` Flow A step 5b posts a comment containing the
  base64-JSON proposal payload via the GitHub MCP. The comment body
  can legitimately exceed 64 KB (large diffs) — if the MCP enforces a
  smaller limit, we need to chunk or attach, and the agent prose
  doesn't handle that today. **Known gap.**
- `fix-and-test` Flow B step 5 opens a PR with title
  `fix(<service>): <summary> [INC-XXXX]`. The MCP should accept head
  branch + base branch + title + body + draft flag. Standard.

**Shakedown concern:** the propose-only comment payload size is the
highest-risk item in this row. If the MCP rejects a large comment, the
whole two-stage flow breaks for any fix with a non-trivial diff.
Verification step on day one: post a dummy 80 KB comment via the MCP
on a throwaway issue and confirm it comes back intact.

## pagerduty (PagerDuty official MCP)

**Connection status in sandbox:** not connected.

**Tools expected from docs:**

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__pagerduty__list_incidents` | 🟡 expected | `signals` step 6 (pages in window) |
| `mcp__pagerduty__get_incident` | 🟡 expected | `signals` (drill into page) |
| `mcp__pagerduty__list_incident_log_entries` | 🟡 expected | `prior-incident` (resolution notes) |

**Agent-prose expectations:**

- `signals` lists incidents where `service.id` matches candidate
  services and `created_at` falls in the window.
- `prior-incident` searches resolved incidents on the same services
  in the last 90 days.

**Shakedown concern:** PagerDuty services are identified by `service.id`,
not service name. Same pattern as Dynatrace entity IDs. The agent
prose currently assumes "service name" is a universal identifier —
the shakedown will reveal whether the MCP does that mapping or whether
we need a name→ID resolution step.

## azure-devops (Microsoft official ADO MCP — local stdio)

**Connection status in sandbox:** not connected.

**Tools expected from docs:**

| Tool | Status | Used by agent |
| ---- | ------ | ------------- |
| `mcp__azure-devops__wit_get_work_item` | 🟡 expected | `intake` (ADO-routed tickets) |
| `mcp__azure-devops__wit_list_work_items` | 🟡 expected | `prior-incident` (resolved tickets on same area) |
| `mcp__azure-devops__wiki_search` | 🟡 expected | `prior-incident` (ADO Wiki postmortems) |
| `mcp__azure-devops__wiki_page` | 🟡 expected | `intake` (linked requirement doc from ADO Wiki) |

**Agent-prose expectations:** same as the Atlassian row but via ADO
objects. `intake` needs to distinguish Jira-shaped IDs
(`PROJ-1234`) from ADO-shaped IDs (`AB#1234` or bare integers) and
route to the right MCP.

**Shakedown concern:** ADO MCP is local stdio (npx). First-run
behavior: npx downloads the package, which adds latency on cold runs
and fails if the runner has no npm registry access. If the pilot
org air-gaps their CI, this row breaks before any tool is invoked.
Known mitigation: pin the version and pre-install in a builder image.

## Summary for the day-one shakedown

Order of priority for the platform engineer watching the first real run:

1. **github** — largest surface, single biggest failure mode is the
   comment-payload-size limit. Run the dummy-comment test first.
2. **datadog** — Watchdog-Insights tool name is the anchor of the
   headline contract. Must verify in init.
3. **dynatrace** — Davis-Problems tool, same reason. Only urgent if
   Dynatrace is the primary observability stack; otherwise lower.
4. **pagerduty** — lowest surface; service-ID mapping is the only
   gotcha.
5. **azure-devops** — only if ADO is the ticketing system.

If atlassian keeps holding, the two verified tools cover the
Confluence + Jira read surface completely.
