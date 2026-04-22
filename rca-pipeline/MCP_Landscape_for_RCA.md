# MCP Landscape for the RCA Accelerator

**Purpose.** Grounded answer to Jothi's ask: research what MCP connectors exist today for the sources she called out (ADO, Confluence, Datadog/Dynatrace, GitHub, PagerDuty), so we can cleanly separate "use what's already there" from "custom build the gap."

**Framing.** Claude Code is the chassis. MCP is how the chassis talks to the enterprise stack. Our IP is only what sits on top of a complete MCP wiring and still adds defensible value. Everything below is mapped to the 4-capability scope Jothi narrowed us down to:

1. Pull the incident ticket / requirement
2. Pull related logs and traces
3. Check prior incidents and runbooks
4. Generate the fix, test it, code-review it

---

## Per-source MCP status (April 2026)

| Source | MCP | Maintainer | Status | Transport / Host | Auth |
|---|---|---|---|---|---|
| Azure DevOps | `microsoft/azure-devops-mcp` | Microsoft (first-party) | Local GA. Remote in public preview; roadmap item for 2026 GA. | Local stdio today; remote HTTP coming. | Entra ID / PAT |
| Confluence + Jira | Atlassian Remote MCP Server ("Rovo MCP") | Atlassian (first-party) | **GA February 2026.** 72+ tools across Jira, Confluence, Compass. Anthropic was the first official partner. | Remote, hosted on Cloudflare. Endpoint moving from `/v1/sse` to `/v1/mcp` by June 30, 2026. | OAuth, respects permission boundaries |
| Confluence + Jira (self-host) | `sooperset/mcp-atlassian` | Community | Active. Works against Cloud, Server, Data Center. | Local / container. | API token / PAT |
| Datadog | Datadog MCP Server | Datadog (first-party) | **GA March 10, 2026.** 16+ core tools + optional toolsets (APM, Error Tracking, Feature Flags, DBM, Security, LLM Obs). | Remote, Datadog-hosted. HIPAA-eligible; not GovCloud compatible. | Datadog API key + app key |
| Datadog (self-host) | `winor30/mcp-server-datadog`, `GeLi2001/datadog-mcp-server`, `shelfio/datadog-mcp` | Community | Multiple active forks. Varying coverage. | Local / container — deployable inside a VPC. | Datadog API credentials |
| Dynatrace | `dynatrace-oss/dynatrace-mcp` | Dynatrace (first-party, open source) | Active. Exposes Davis AI, DQL queries against Grail, problem events, logs, metrics, spans. Davis CoPilot can convert natural language → DQL. Includes a `verify_dql` tool. | Local / container. Docker image on Docker MCP Catalog. | OAuth / Dynatrace token |
| GitHub | `github/github-mcp-server` | GitHub (first-party) | Active. Deep coverage: PRs, issues, commits, code search, Actions, releases. | Local Docker is primary. Remote exists (`api.githubcopilot.com/mcp`) via PAT. Remote OAuth for Claude Code not there yet. GitHub Enterprise Server does **not** support remote hosting — must run local. | OAuth App / GitHub App / PAT |
| PagerDuty | `PagerDuty/pagerduty-mcp-server` | PagerDuty (first-party) | Active. **March 27, 2026** update added time-range filtering ("last 24h"), pagination for on-call queries, assignee-on-create. Already wired into Azure SRE Agent and has a Cursor plug-in. | Local. | PagerDuty API key |

**Takeaway:** every source Jothi named has a first-party or near-first-party MCP available today. None of them require us to build a connector from scratch. For any enterprise that needs data to stay inside their VPC, a self-hosted container exists for every source on the list.

---

## Mapping to Jothi's 4-capability scope

### 1. Pull the incident ticket / requirement

- **Primary:** Atlassian Remote MCP (Jira) or Azure DevOps MCP (work items), depending on HGV's stack.
- **Secondary:** Atlassian Remote MCP (Confluence) to pull the linked requirement doc once we know which module the incident belongs to.
- **Covered out of the box:** ticket fetch, linked wiki/Confluence pages, assignees, labels, history.
- **What's missing:** **requirement-to-incident linkage when the ticket doesn't spell out the module.** The MCP returns whatever the ticket says. If the ticket says "checkout is broken," no MCP tells us which service owns checkout or which requirement doc governs it. This is an IP opportunity (a module-routing agent / skill).

### 2. Pull related logs and traces

- **Primary:** Datadog MCP or Dynatrace MCP, whichever HGV uses. Both expose logs, traces, metrics, incidents, problems.
- **Covered out of the box:** natural-language query → DQL (Dynatrace Davis CoPilot) or structured log/metric/trace retrieval (Datadog). Both vendors' AIOps/anomaly detection is accessible via MCP.
- **What's missing:** **time-window selection.** This is the gap Jothi explicitly flagged. The MCP will return logs for any window you ask for — but picking the right window from a ticket with no timestamp is still on us. Datadog AIOps and Davis AI surface anomalies inside a window; neither picks the window from a free-text incident description. This is the single highest-value gap to fill.

### 3. Check prior incidents and runbooks

- **Primary:** Atlassian Remote MCP (Confluence search) for postmortems and runbooks. PagerDuty MCP for incident history.
- **Covered out of the box:** keyword / semantic search across Confluence spaces, incident timeline from PagerDuty.
- **What's missing, nuanced:** search-quality and ranking. Confluence search returns whatever Confluence ranks first. If we want deterministic BM25/TFIDF reranking of MCP-returned results, that remains a skill. **But do not build a vector store of postmortems** — Jothi was explicit. Query live via MCP, rerank on read.

### 4. Generate the fix, test it, code-review it

- **Primary:** Claude Code natively + GitHub MCP.
- **Covered out of the box:** repo access, diff generation, PR creation, test execution via Actions, code review via `anthropics/claude-code-action@v1`.
- **What's missing:** essentially nothing Jothi asked for. Claude Code is the mature runtime here — this is the least defensible place to build custom.

---

## Where our IP actually earns its keep

Based on the gap analysis above, the defensible custom surface collapses to three things:

1. **Time-window selection.** Given a free-text incident ticket with no timestamp, pick the log/trace window most likely to contain the root cause. CUSUM / change-point detection over the MCP-returned metric stream, cross-referenced with deploy events from GitHub MCP and paging events from PagerDuty MCP. **This is the headline IP.** Jothi explicitly said nobody has solved this.
2. **Module routing.** Given a vague ticket ("checkout is broken"), route to the right service and the right Confluence requirement doc. Combines code-ownership data (GitHub MCP) with service topology and requirement-doc tags (Confluence MCP).
3. **Cross-agent validation gate.** Claude Code has no native cross-agent validation. When the logs agent says "it's the database" and the trace agent says "it's the queue," a deterministic validator catches the contradiction before the fix agent acts on it. Ships as a `.claude/skills/` or a dedicated validator sub-agent.

Everything else from the original 18-agent design either (a) collapses into an MCP call, (b) collapses into a skill that reranks MCP output, or (c) gets dropped because Claude Code already does it.

---

## Enterprise deployment considerations (for HGV and future customers)

- **Data residency.** Datadog's hosted MCP is not GovCloud-compatible; Atlassian's runs on Cloudflare; Microsoft's remote ADO MCP is in preview. For customers that require data to stay inside their VPC, plan to use the self-hosted community or open-source MCPs (Datadog has three viable ones, Dynatrace's is open source, Atlassian has sooperset). Call this out in every enterprise pitch.
- **GitHub Enterprise Server** customers must run the local GitHub MCP — remote hosting is not supported.
- **Atlassian endpoint migration.** `/v1/sse` sunsets June 30, 2026. Anything we build needs to point at `/v1/mcp`.
- **Audit.** Datadog MCP logs every tool call to Datadog Audit Trail with user identity and MCP client. Good for enterprise compliance story; we should mirror this pattern for any custom MCP or skill we add.

---

## Recommended next steps

1. **Stop building custom signal collectors.** Kill the log/APM/trace/deploy agents in the Python orchestrator. Wire the equivalent MCPs into a minimal Claude Code project.
2. **Prototype Jothi's 4-capability scope on Claude Code + MCPs alone.** Entry point: `.claude/commands/rca.md` → `/rca INC-1234`. Agents: intake, signals, prior-incident, fix-and-test. Measure what works and what doesn't before adding anything custom.
3. **Solve time-window selection as the first custom skill.** This is the defensible IP. Implement CUSUM over Datadog/Dynatrace metric streams, with deploy/page events as priors. Ship as `.claude/skills/time-window-selector/`.
4. **Port deterministic algorithms that survive the cut** (BM25 rerank, DBSCAN alert clustering, cross-agent validator) into `.claude/skills/` — only the ones that have a clear answer to "why not just use Claude Code."
5. **Bring the benchmark plan back to Jothi with this wiring.** Head-to-head on cost per incident, time-window selection accuracy, and hallucination rate on contradictory signals — axes where the custom layer has a real shot.

---

## Sources

- [microsoft/azure-devops-mcp on GitHub](https://github.com/microsoft/azure-devops-mcp)
- [Azure DevOps MCP Server overview (Microsoft Learn)](https://learn.microsoft.com/en-us/azure/devops/mcp-server/mcp-server-overview?view=azure-devops)
- [Remote Azure DevOps MCP Server (Microsoft Learn)](https://learn.microsoft.com/en-us/azure/devops/mcp-server/remote-mcp-server?view=azure-devops)
- [Azure DevOps Remote MCP Server public preview (DevBlogs)](https://devblogs.microsoft.com/devops/azure-devops-remote-mcp-server-public-preview/)
- [atlassian/atlassian-mcp-server on GitHub](https://github.com/atlassian/atlassian-mcp-server)
- [Introducing Atlassian's Remote MCP Server](https://www.atlassian.com/blog/announcements/remote-mcp-server)
- [Atlassian Rovo MCP Server support docs](https://support.atlassian.com/atlassian-rovo-mcp-server/docs/use-atlassian-rovo-mcp-server/)
- [sooperset/mcp-atlassian on GitHub](https://github.com/sooperset/mcp-atlassian)
- [Datadog MCP Server docs](https://docs.datadoghq.com/bits_ai/mcp_server/)
- [Datadog remote MCP server announcement](https://www.datadoghq.com/blog/datadog-remote-mcp-server/)
- [winor30/mcp-server-datadog on GitHub](https://github.com/winor30/mcp-server-datadog)
- [dynatrace-oss/dynatrace-mcp on GitHub](https://github.com/dynatrace-oss/dynatrace-mcp)
- [Dynatrace MCP Server overview](https://www.dynatrace.com/platform/mcp-server/)
- [github/github-mcp-server on GitHub](https://github.com/github/github-mcp-server)
- [Install GitHub MCP for Claude Code](https://github.com/github/github-mcp-server/blob/main/docs/installation-guides/install-claude.md)
- [PagerDuty/pagerduty-mcp-server on GitHub](https://github.com/PagerDuty/pagerduty-mcp-server)
- [PagerDuty MCP Server integration guide](https://support.pagerduty.com/main/docs/pagerduty-mcp-server-integration-guide)
- [PagerDuty: Incidents two ways with MCP](https://www.pagerduty.com/blog/ai/incidents-two-ways-with-mcp/)
