#!/usr/bin/env python3
"""
verify_mcp_config — structural + env-var sanity check on .mcp.json.

What it checks:
  1. .mcp.json is valid JSON with an mcpServers object.
  2. Every server has a supported type (http / stdio) and the right fields.
  3. Every ${VAR} reference in url / headers / env actually exists in the
     environment (or is whitelisted as "optional for dry-run").
  4. For stdio servers, the command binary is resolvable on PATH.

What it does NOT do:
  - Actually open a session to the MCP server. That needs live credentials
    and live network — out of scope for this offline check.

Exit codes:
  0  all checks pass
  1  one or more checks failed
  2  usage / unreadable config
"""
from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path


VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def collect_vars(obj) -> set[str]:
    """Walk a config tree, collect every ${VAR} reference."""
    vars_: set[str] = set()
    if isinstance(obj, str):
        vars_.update(VAR_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            vars_ |= collect_vars(v)
    elif isinstance(obj, list):
        for v in obj:
            vars_ |= collect_vars(v)
    return vars_


def verify(config_path: Path, *, require_env: bool) -> int:
    if not config_path.exists():
        print(f"ERROR: {config_path} does not exist", file=sys.stderr)
        return 2
    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {config_path} is not valid JSON: {e}", file=sys.stderr)
        return 2

    servers = config.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        print("ERROR: mcpServers missing or empty", file=sys.stderr)
        return 1

    failures: list[str] = []
    warnings: list[str] = []

    print(f"Checking {len(servers)} MCP server(s) in {config_path}\n")

    for name, spec in servers.items():
        if not isinstance(spec, dict):
            failures.append(f"{name}: spec must be an object")
            continue

        kind = spec.get("type")
        if kind not in ("http", "stdio"):
            failures.append(f"{name}: unsupported type '{kind}' (want http|stdio)")
            continue

        if kind == "http":
            if not spec.get("url"):
                failures.append(f"{name}: http server missing 'url'")
        else:  # stdio
            cmd = spec.get("command")
            if not cmd:
                failures.append(f"{name}: stdio server missing 'command'")
            elif shutil.which(cmd) is None:
                warnings.append(
                    f"{name}: command '{cmd}' not on PATH (fine if CI installs it first)"
                )

        # Env-var reference sanity
        missing = []
        for var in sorted(collect_vars(spec)):
            if not os.environ.get(var):
                missing.append(var)
        if missing:
            msg = f"{name}: env vars not set: {', '.join(missing)}"
            if require_env:
                failures.append(msg)
            else:
                warnings.append(msg)

        status = "OK" if name not in {f.split(":",1)[0] for f in failures} else "FAIL"
        print(f"  [{status:<4s}] {name:<14s} type={kind}")

    print()
    for w in warnings:
        print(f"  [WARN] {w}")
    for f in failures:
        print(f"  [FAIL] {f}")

    if failures:
        print(f"\nFAILED: {len(failures)} structural issue(s)")
        return 1
    if warnings and require_env:
        return 1
    print(f"\nOK: {len(servers)} server(s) structurally valid" +
          (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=".mcp.json", help="Path to .mcp.json")
    ap.add_argument(
        "--require-env", action="store_true",
        help="Treat missing env vars as failures (use in live-deploy CI, not PR checks)",
    )
    args = ap.parse_args()
    return verify(Path(args.config), require_env=args.require_env)


if __name__ == "__main__":
    sys.exit(main())
