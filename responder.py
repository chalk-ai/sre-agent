"""Receives Datadog incident webhooks and starts one sandboxed investigation per incident.

Deployed by deploy_responder.py as a small Chalk ScalingGroup. Point a Datadog webhook at
POST /webhooks/datadog with an `X-Webhook-Secret` header (see the README for the payload).

    POST /webhooks/datadog   start an investigation; returns 202 with a run id
    GET  /runs/{run_id}      the run's status and trace so far (same secret header)

Each investigation runs in its own sandbox (investigation.py), so the responder itself holds
the credentials but never runs model-chosen code.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
import uuid
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request

from investigation import DEFAULT_MODEL, investigate

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
MODEL = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_INVESTIGATIONS", "2"))
SERVICES = {k: os.environ[k] for k in ("OPS_MCP_URL", "OPS_MCP_TOKEN", "DD_API_URL", "DD_API_KEY", "DD_APP_KEY")}

app = FastAPI(title="sre-agent-responder")
runs: dict[str, dict[str, Any]] = {}
by_incident: dict[str, str] = {}
lock = threading.Lock()
slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def authorize(request: Request) -> None:
    supplied = request.headers.get("x-webhook-secret", "")
    if not (WEBHOOK_SECRET and hmac.compare_digest(supplied, WEBHOOK_SECRET)):
        raise HTTPException(status_code=401, detail="bad webhook secret")


def run_investigation(run_id: str, webhook: dict[str, Any]) -> None:
    run = runs[run_id]
    with slots:
        run["status"] = "running"
        try:
            code = investigate(webhook, SERVICES, MODEL, run["events"].append)
            run["status"] = "done" if code == 0 else f"failed (exit {code})"
        except Exception as e:
            run["status"] = f"failed ({type(e).__name__}: {e})"
        run["finished_at"] = time.time()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "runs": len(runs)}


@app.post("/webhooks/datadog", status_code=202, dependencies=[Depends(authorize)])
async def datadog_webhook(webhook: dict[str, Any], background: BackgroundTasks) -> dict[str, Any]:
    incident = str(webhook.get("incident_id") or webhook.get("public_id") or "")
    if not incident:
        raise HTTPException(status_code=400, detail="payload has no incident_id")
    if webhook.get("event", "incident.created") != "incident.created":
        return {"ignored": webhook.get("event")}
    with lock:
        # Datadog retries deliveries; one incident gets one investigation.
        if incident in by_incident:
            return {"run_id": by_incident[incident], "duplicate": True}
        run_id = uuid.uuid4().hex[:12]
        runs[run_id] = {"incident": incident, "status": "queued", "started_at": time.time(), "events": []}
        by_incident[incident] = run_id
    background.add_task(run_investigation, run_id, webhook)
    return {"run_id": run_id}


@app.get("/runs/{run_id}", dependencies=[Depends(authorize)])
async def get_run(run_id: str) -> dict[str, Any]:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no such run")
    return run


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)
