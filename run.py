#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.5", "httpx==0.28.1"]
# ///
"""Investigate one incident from your machine: the same sandboxed run the webhook responder starts.

    ./run.py [incidents/webhook.json] [--model anthropic/claude-sonnet-5-5]

Needs .services.env from deploy_services.py, and Chalk credentials from `chalk config` (or the
CHALK_* environment variables). Writes the run's trace to traces/<timestamp>.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from investigation import DEFAULT_MODEL, investigate  # noqa: E402


def load_chalk_env(env_id: str | None) -> None:
    if "CHALK_CLIENT_ID" not in os.environ:
        cfg = json.loads(subprocess.check_output(["chalk", "config", "--json"]))
        os.environ["CHALK_API_SERVER"] = cfg["apiServer"]["value"]
        os.environ["CHALK_CLIENT_ID"] = cfg["clientId"]["value"]
        os.environ["CHALK_CLIENT_SECRET"] = cfg["clientSecret"]["value"]
        os.environ.setdefault("CHALK_ENVIRONMENT_ID", cfg["environment"]["value"])
    if env_id:
        os.environ["CHALK_ENVIRONMENT_ID"] = env_id


def load_services() -> dict[str, str]:
    env_file = HERE / ".services.env"
    if not env_file.exists():
        sys.exit("Run ./deploy_services.py first.")
    pairs = (line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def render(event: dict[str, Any]) -> str:
    kind = event["type"]
    if kind == "incident":
        w = event["webhook"]
        return f"\n! Incident #{w.get('public_id')} [{w.get('severity')}] {w.get('title')}   ({event['model']})\n"
    if kind == "policy":
        return f"  [policy] {event['request']:<44} {event['outcome']}"
    if kind == "mcp_tools":
        return f"  [mcp] {event['server']}: {', '.join(t['name'] for t in event['tools'])}"
    if kind == "thought":
        return f"\n  {event['text'].strip()}\n"
    if kind == "tool_call":
        args = ", ".join(f"{k}={json.dumps(v)}" for k, v in event["args"].items())
        return f"  → {event['source']}.{event['name']}({args[:160]})"
    if kind == "tool_result":
        flag = "ERROR " if event["is_error"] else ""
        return f"    ← {event['ms']} ms  {flag}{event['result'][:110]}"
    if kind == "datadog_write":
        return f"  [datadog] {event['action']}"
    if kind == "findings":
        lines = ["", f"Root cause ({event['confidence']}): {event['root_cause']}", "", event["summary"], ""]
        lines += [f"  ruled out: {r['hypothesis']}" for r in event["ruled_out"]]
        m = event["mitigation"]
        lines += ["", f"  mitigation: {m['action']} [{m['status']}] {m.get('change_request', '')}"]
        return "\n".join(lines)
    if kind == "stderr":
        return "  [stderr] " + event["text"].rstrip()
    return json.dumps(event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("webhook", nargs="?", default=str(HERE / "incidents" / "webhook.json"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--env-id", default=None)
    args = parser.parse_args()

    load_chalk_env(args.env_id)
    services = load_services()
    webhook = json.loads(Path(args.webhook).read_text())

    # The incidents stand-in keeps what each run writes; start from the fixture again.
    httpx.post(
        f"{services['DD_API_URL']}/demo/reset",
        headers={"DD-API-KEY": services["DD_API_KEY"], "DD-APPLICATION-KEY": services["DD_APP_KEY"]},
        timeout=30,
    ).raise_for_status()

    traces = HERE / "traces"
    traces.mkdir(exist_ok=True)
    trace_path = traces / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    print("Starting sandbox...")
    with trace_path.open("w") as trace:

        def record(event: dict[str, Any]) -> None:
            trace.write(json.dumps(event) + "\n")
            trace.flush()
            print(render(event), flush=True)

        exit_code = investigate(webhook, services, args.model, record)
    print(f"\ntrace: {trace_path.relative_to(HERE)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
