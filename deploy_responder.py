#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute>=2.13.5"]
# ///
"""Deploy the webhook responder: a small ScalingGroup that starts an investigation per incident.

Run deploy_services.py first; this reads .services.env and adds WEBHOOK_SECRET and
RESPONDER_URL to it. Point a Datadog webhook at $RESPONDER_URL/webhooks/datadog with the
header `X-Webhook-Secret: $WEBHOOK_SECRET`.

    ./deploy_responder.py [--env-id <environment>]

The responder starts sandboxes with the Chalk client credentials this script deploys it with.
Give it a service token scoped to the one environment rather than a personal one.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from deploy_services import ENV_FILE, load_chalk_env, read_env_file  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-id", default=None)
    parser.add_argument("--name", default="sre-agent-responder")
    args = parser.parse_args()
    load_chalk_env(args.env_id)
    from chalkcompute import Image, ReadinessProbe, ScalingGroup, Secret

    saved = read_env_file()
    if "OPS_MCP_URL" not in saved:
        sys.exit("Run ./deploy_services.py first.")
    saved["WEBHOOK_SECRET"] = saved.get("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    secret_keys = ("OPS_MCP_TOKEN", "DD_API_KEY", "DD_APP_KEY", "WEBHOOK_SECRET")
    for key in secret_keys:
        os.environ[f"DEPLOY_{key}"] = saved[key]

    image = (
        Image.debian_slim(python_version="3.12")
        .pip_install(["chalkcompute>=2.13.5", "fastapi==0.116.1", "uvicorn==0.35.0", "httpx==0.28.1"])
        .add_local_file(str(HERE / "responder.py"), "/app/responder.py", strategy="copy")
        .add_local_file(str(HERE / "investigation.py"), "/app/investigation.py", strategy="copy")
        .add_local_file(str(HERE / "agent.py"), "/app/agent.py", strategy="copy")
        .workdir("/app")
    )
    responder = ScalingGroup(
        image=image,
        name=args.name,
        cpu="500m",
        memory="1Gi",
        env={
            "CHALK_API_SERVER": os.environ["CHALK_API_SERVER"],
            "CHALK_ENVIRONMENT_ID": os.environ["CHALK_ENVIRONMENT_ID"],
            "CHALK_CLIENT_ID": Secret.from_local_env("CHALK_CLIENT_ID"),
            "CHALK_CLIENT_SECRET": Secret.from_local_env("CHALK_CLIENT_SECRET"),
            "OPS_MCP_URL": saved["OPS_MCP_URL"],
            "DD_API_URL": saved["DD_API_URL"],
            **{key: Secret.from_local_env(f"DEPLOY_{key}") for key in secret_keys},
        },
        entrypoint=["python", "/app/responder.py"],
        port=8080,
        readiness_probe=ReadinessProbe.http("/healthz"),
        min_replicas=1,
        max_replicas=1,
    ).deploy(ready_timeout=900)
    saved["RESPONDER_URL"] = str(responder.web_url).rstrip("/")
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in sorted(saved.items())))
    print(f"responder: {saved['RESPONDER_URL']}/webhooks/datadog")
    print(f"wrote {ENV_FILE.name}")


if __name__ == "__main__":
    main()
