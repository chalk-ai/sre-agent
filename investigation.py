"""Start one investigation: a fresh Chalk sandbox running agent.py against one incident.

Shared by run.py (from your laptop) and responder.py (from a Datadog webhook). Both pass the
incident webhook payload and a callback that receives the run's trace, one event at a time.

What the sandbox can reach is the point:
  - its network policy allows exactly three hosts: the Chalk API (model router), the internal
    ops MCP server and the Datadog API;
  - each credential is bound to the host it belongs to. Inside the sandbox its environment
    variable holds a random placeholder, and the egress proxy swaps in the real value only on
    requests to that host. The agent cannot read a token, and cannot send one anywhere else.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"

# Runs in the sandbox before the agent: tokens are placeholders, hosts outside the allowlist are
# unreachable, and an allowed host still rejects a request that carries no token.
POLICY_PROBE = """
import os, httpx

def post(url, **kw):
    try:
        return str(httpx.post(url, timeout=10, **kw).status_code)
    except Exception as e:
        return f"blocked ({type(e).__name__})"

print("$OPS_MCP_TOKEN in the sandbox|" + os.environ["OPS_MCP_TOKEN"][:12] + "... (placeholder)")
print("POST https://api.github.com/gists|" + post("https://api.github.com/gists"))
print("POST https://hooks.slack.com/services/x|" + post("https://hooks.slack.com/services/x"))
print("POST OPS_MCP_URL (no token)|" + post("OPS_MCP_URL", json={}))
"""


def investigate(
    webhook: dict[str, Any],
    services: dict[str, str],
    model: str,
    record: Callable[[dict[str, Any]], None],
) -> int:
    """Runs the agent on one incident in a new sandbox, streaming its trace to `record`.

    `services` holds OPS_MCP_URL, OPS_MCP_TOKEN, DD_API_URL, DD_API_KEY and DD_APP_KEY.
    Returns the agent's exit code."""
    from chalkcompute import ConnectClient, Image, NetworkPolicy, Sandbox, SandboxClient, Secret

    connect = ConnectClient()
    try:
        connect._ensure_auth()
        api = connect._api_server.rstrip("/")
        env_id = connect._env_id
        # The egress proxy needs a bearer for this host; the SDK owns its authentication.
        os.environ["CHALK_API_TOKEN"] = connect._access_token
    finally:
        connect.close()
    for key in ("OPS_MCP_TOKEN", "DD_API_KEY", "DD_APP_KEY"):
        os.environ[key] = services[key]
    hosts = {
        "chalk": urlsplit(api).hostname,
        "ops": urlsplit(services["OPS_MCP_URL"]).hostname,
        "datadog": urlsplit(services["DD_API_URL"]).hostname,
    }

    image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["mcp==2.3.0", "httpx==0.28.1"])
        .add_local_file(str(HERE / "agent.py"), "/app/agent.py", strategy="copy")
    )
    sandbox = (
        Sandbox(
            image=image,
            name=f"sre-agent-{int(time.time())}",
            cpu="1",
            memory="2Gi",
            lifetime="900s",
            # An empty allowlist: the three hosts below are the only egress.
            network_policy=NetworkPolicy(allowed_hosts=[]),
        )
        .add_credentials(hosts["chalk"], Secret.from_local_env("CHALK_API_TOKEN"))
        .add_credentials(hosts["ops"], Secret.from_local_env("OPS_MCP_TOKEN"))
        .add_credentials(hosts["datadog"], Secret.from_local_env("DD_API_KEY"), Secret.from_local_env("DD_APP_KEY"))
    )
    env = {
        "CHALK_API_HOST": api,
        "CHALK_ENVIRONMENT_ID": env_id,
        "OPS_MCP_URL": services["OPS_MCP_URL"],
        "DD_API_URL": services["DD_API_URL"],
        "AGENT_MODEL": model,
        "PYTHONUNBUFFERED": "1",
    }

    client = SandboxClient.from_env()
    exit_code = 1
    try:
        client.run(sandbox)
        probe = sandbox.exec(
            "python3", "-c", POLICY_PROBE.replace("OPS_MCP_URL", services["OPS_MCP_URL"]), env=env, timeout_secs=60
        )
        for line in probe.stdout_text.splitlines():
            request, _, outcome = line.partition("|")
            record({"type": "policy", "request": request, "outcome": outcome or "-"})

        # Output arrives in arbitrary chunks; parse only complete lines.
        pending = ""
        for event in sandbox.exec_stream("python3", "/app/agent.py", json.dumps(webhook), env=env, timeout_secs=600):
            pending += event.stdout or ""
            *lines, pending = pending.split("\n")
            for line in lines:
                if line.startswith("{"):
                    record(json.loads(line))
            if event.stderr:
                record({"type": "stderr", "text": event.stderr})
            if event.exit_code is not None:
                exit_code = event.exit_code
        return exit_code
    finally:
        if sandbox.id is not None:
            sandbox.terminate()
