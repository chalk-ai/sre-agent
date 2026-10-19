#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.5"]
# ///
"""Deploy the internal ops MCP server and the Datadog API stand-in as two Chalk ScalingGroups.

Each gets its own random credentials. URLs and credentials are written to .services.env
(gitignored), where run.py and responder.py pick them up. Re-running reuses the credentials.

    ./deploy_services.py [--env-id <environment>]
"""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".services.env"


def read_env_file() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    pairs = (line.split("=", 1) for line in ENV_FILE.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-id", default=None)
    parser.add_argument("--prefix", default="halyard")
    args = parser.parse_args()
    if args.env_id:
        os.environ["CHALK_ENVIRONMENT"] = args.env_id
    from chalkcompute import Image, ReadinessProbe, ScalingGroup, Secret

    saved = read_env_file()
    for key in ("OPS_MCP_TOKEN", "DD_API_KEY", "DD_APP_KEY"):
        saved[key] = saved.get(key) or secrets.token_urlsafe(32)
        os.environ[f"DEPLOY_{key}"] = saved[key]

    ops_image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["mcp==2.3.0", "duckdb==1.4.1", "pytz==2025.2", "uvicorn==0.35.0"])
        # Mounted from a volume rather than inlined: together the two files exceed the 32 KiB
        # that strategy="copy" allows.
        .add_local_dir(str(HERE / "ops"), "/app", strategy="volume", exclude=["__pycache__/**"])
        .workdir("/app")
    )
    ops = ScalingGroup(
        image=ops_image,
        name=f"{args.prefix}-ops-mcp",
        cpu="500m",
        memory="1Gi",
        env={"OPS_MCP_TOKEN": Secret.from_local_env("DEPLOY_OPS_MCP_TOKEN")},
        entrypoint=["python", "/app/server.py"],
        port=8080,
        readiness_probe=ReadinessProbe.http("/healthz"),
        min_replicas=1,
        max_replicas=1,
    ).deploy(ready_timeout=900)
    saved["OPS_MCP_URL"] = str(ops.web_url).rstrip("/") + "/mcp"
    print(f"ops MCP server: {saved['OPS_MCP_URL']}")

    incidents_image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["fastapi==0.116.1", "uvicorn==0.35.0"])
        .add_local_file(str(HERE / "incidents" / "server.py"), "/app/server.py", strategy="copy")
        .add_local_file(str(HERE / "incidents" / "incident.json"), "/app/incident.json", strategy="copy")
        .workdir("/app")
    )
    incidents = ScalingGroup(
        image=incidents_image,
        name=f"{args.prefix}-incidents-api",
        cpu="250m",
        memory="512Mi",
        env={
            "DD_API_KEY": Secret.from_local_env("DEPLOY_DD_API_KEY"),
            "DD_APP_KEY": Secret.from_local_env("DEPLOY_DD_APP_KEY"),
        },
        entrypoint=["python", "/app/server.py"],
        port=8080,
        readiness_probe=ReadinessProbe.http("/healthz"),
        min_replicas=1,
        max_replicas=1,
    ).deploy(ready_timeout=900)
    saved["DD_API_URL"] = str(incidents.web_url).rstrip("/")
    print(f"Datadog API stand-in: {saved['DD_API_URL']}")

    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in sorted(saved.items())))
    print(f"wrote {ENV_FILE.name}")


if __name__ == "__main__":
    main()
