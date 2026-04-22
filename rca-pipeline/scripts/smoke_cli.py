"""Smoke test for the Claude Code CLI invocation the workflow uses.

This is the regression gate that catches the class of bug that delayed
the pilot by a day: flag names written from memory ("--headless",
"--max-turns") that don't exist. The workflow's CLI invocation is the
single seam where the chassis meets the harness, and it runs so deep
in the pipeline that a flag bug only surfaces during a real incident.

What this script does:

1. Runs the real `claude` CLI with the exact flag set the workflow
   uses (minus anything that requires a live API key).
2. Parses the first line of stream-json output — the `init` event —
   which Claude Code emits unconditionally, before it tries to auth.
3. Asserts the init event reports the chassis topology we expect:
   all custom agents registered, all custom skills registered, `/rca`
   command registered, `.mcp.json` detected, permissionMode is
   bypassPermissions.

This runs without an API key. The CLI fails shortly after init with
"Not logged in", but init emits first. We inspect init and exit 0 —
an auth failure after init is expected here.

Exit codes:
    0 — init block reports the expected chassis
    1 — CLI invocation didn't parse (flag bug, binary not installed)
    2 — init block is missing some expected agent/skill/command
    3 — init block reports wrong permissionMode or other config mismatch
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

# What the workflow actually runs. Keep this in sync with
# .github/workflows/rca.yml's "Run Claude Code headless RCA" step —
# if those flags drift, this smoke test no longer represents what CI
# invokes.
WORKFLOW_FLAGS = [
    "--print",
    "--output-format", "stream-json",
    "--verbose",
    "--permission-mode", "bypassPermissions",
]

# What the chassis promises to register. These names are the contract
# between the workflow, the agent/skill files, and this smoke test.
# If an agent file gets renamed, this list has to change with it —
# the failure mode is "smoke test catches it immediately".
EXPECTED_AGENTS = {
    "orchestrator",
    "intake",
    "signals",
    "prior-incident",
    "fix-and-test",
    "validator",
}

EXPECTED_SKILLS = {
    "time-window-selector",
    "bm25-rerank",
    "anomaly-ensemble",
    "cross-agent-validator",
    "module-router",
}

EXPECTED_COMMANDS = {"rca"}


def run_cli(cwd: pathlib.Path) -> list[dict]:
    """Run the CLI and return parsed JSON events from stdout."""
    proc = subprocess.run(
        ["claude", *WORKFLOW_FLAGS, "/rca INC-DEMO-001"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # The CLI exits nonzero when auth fails, which is expected here.
    # But if it exits nonzero *before* emitting any init event, that's
    # a flag-parse failure and we should bubble it up.
    events: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            # Not JSON — skip. The CLI sometimes prints a banner.
            continue
    if not events:
        print("::error::CLI produced no JSON events.", file=sys.stderr)
        print(f"stdout:\n{proc.stdout}", file=sys.stderr)
        print(f"stderr:\n{proc.stderr}", file=sys.stderr)
        sys.exit(1)
    return events


def find_init(events: list[dict]) -> dict:
    for e in events:
        if e.get("type") == "system" and e.get("subtype") == "init":
            return e
    print("::error::No init event found in CLI output.", file=sys.stderr)
    sys.exit(1)


def check(init: dict) -> int:
    """Assert the chassis topology. Return an exit code."""
    failures: list[str] = []

    agents = set(init.get("agents", []))
    missing_agents = EXPECTED_AGENTS - agents
    if missing_agents:
        failures.append(f"missing agents: {sorted(missing_agents)}")

    skills = set(init.get("skills", []))
    missing_skills = EXPECTED_SKILLS - skills
    if missing_skills:
        failures.append(f"missing skills: {sorted(missing_skills)}")

    commands = set(init.get("slash_commands", []))
    missing_commands = EXPECTED_COMMANDS - commands
    if missing_commands:
        failures.append(f"missing slash commands: {sorted(missing_commands)}")

    if init.get("permissionMode") != "bypassPermissions":
        failures.append(
            f"expected permissionMode=bypassPermissions, "
            f"got {init.get('permissionMode')!r}"
        )

    mcp_servers = {s.get("name") for s in init.get("mcp_servers", [])}
    expected_mcps = {"atlassian", "azure-devops", "datadog",
                     "dynatrace", "github", "pagerduty"}
    missing_mcps = expected_mcps - mcp_servers
    if missing_mcps:
        # Missing = configured in .mcp.json but not detected by the CLI.
        # This would indicate a config-file parse error. "Failed to
        # connect" is fine (no creds in this sandbox); "not listed at
        # all" is the bug.
        failures.append(f"MCP servers not detected by CLI: {sorted(missing_mcps)}")

    if failures:
        print("::error::Chassis smoke test failed:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 2 if any("missing" in f for f in failures) else 3

    print("Chassis smoke OK:")
    print(f"  agents:   {sorted(agents & EXPECTED_AGENTS)}")
    print(f"  skills:   {sorted(skills & EXPECTED_SKILLS)}")
    print(f"  commands: {sorted(commands & EXPECTED_COMMANDS)}")
    print(f"  mcp:      {sorted(mcp_servers & expected_mcps)}")
    print(f"  permissionMode: {init.get('permissionMode')}")
    return 0


def main() -> int:
    # Run from the repo root so .mcp.json and .claude/ are picked up.
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    events = run_cli(repo_root)
    init = find_init(events)
    return check(init)


if __name__ == "__main__":
    sys.exit(main())
