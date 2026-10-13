"""A small stand-in for the Datadog Incidents and Monitors APIs, served from one fixture.

It accepts the same request shapes and auth headers as the real API for the handful of calls the
agent makes, so pointing the agent at api.datadoghq.com instead needs no code change. Writes
(timeline notes, incident field updates) are kept in memory so a run's output can be inspected.

    GET   /api/v2/incidents/{incident_id}            incident, by UUID or public id
    GET   /api/v1/monitor/{monitor_id}               the monitor that opened it
    GET   /api/v2/incidents/{incident_id}/timeline   notes posted so far
    POST  /api/v2/incidents/{incident_id}/timeline   post a markdown note
    PATCH /api/v2/incidents/{incident_id}            update incident fields (e.g. root cause)

Every request needs DD-API-KEY and DD-APPLICATION-KEY headers matching DD_API_KEY and
DD_APP_KEY, and is logged as one JSON line.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel

API_KEY = os.environ.get("DD_API_KEY", "")
APP_KEY = os.environ.get("DD_APP_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
FIXTURE = json.loads((Path(__file__).parent / "incident.json").read_text())

logging.basicConfig(level=logging.INFO, format="%(message)s")
audit = logging.getLogger("audit")

app = FastAPI(title="incidents-api-stand-in")
state: dict[str, Any] = {}


def reset() -> None:
    state["incident"] = copy.deepcopy(FIXTURE["incident"])
    state["timeline"] = []


reset()


def authorize(request: Request) -> None:
    ok = (
        bool(API_KEY)
        and bool(APP_KEY)
        and hmac.compare_digest(request.headers.get("dd-api-key", ""), API_KEY)
        and hmac.compare_digest(request.headers.get("dd-application-key", ""), APP_KEY)
    )
    audit.info(
        json.dumps({"ts": round(time.time(), 3), "method": request.method, "path": request.url.path, "authorized": ok})
    )
    if not ok:
        raise HTTPException(status_code=403, detail="Forbidden")


def incident_or_404(incident_id: str) -> dict[str, Any]:
    inc = state["incident"]
    if incident_id not in (inc["id"], str(inc["attributes"]["public_id"])):
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    return inc


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/v2/incidents/{incident_id}", dependencies=[Depends(authorize)])
async def get_incident(incident_id: str) -> dict[str, Any]:
    return {"data": incident_or_404(incident_id)}


@app.get("/api/v1/monitor/{monitor_id}", dependencies=[Depends(authorize)])
async def get_monitor(monitor_id: int) -> dict[str, Any]:
    if monitor_id != FIXTURE["monitor"]["id"]:
        raise HTTPException(status_code=404, detail=f"monitor {monitor_id} not found")
    return FIXTURE["monitor"]


@app.get("/api/v2/incidents/{incident_id}/timeline", dependencies=[Depends(authorize)])
async def get_timeline(incident_id: str) -> dict[str, Any]:
    incident_or_404(incident_id)
    return {"data": state["timeline"]}


class CellContent(BaseModel):
    content: str


class CellAttributes(BaseModel):
    cell_type: str = "markdown"
    content: CellContent


class CellData(BaseModel):
    attributes: CellAttributes


class CellRequest(BaseModel):
    data: CellData


@app.post("/api/v2/incidents/{incident_id}/timeline", dependencies=[Depends(authorize)])
async def post_timeline(incident_id: str, req: CellRequest) -> dict[str, Any]:
    incident_or_404(incident_id)
    if len(req.data.attributes.content.content) > 20_000:
        raise HTTPException(status_code=400, detail="note too long")
    cell = {
        "id": f"cell-{len(state['timeline']) + 1}",
        "type": "incident_timeline_cells",
        "attributes": {
            "cell_type": req.data.attributes.cell_type,
            "content": {"content": req.data.attributes.content.content},
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    state["timeline"].append(cell)
    return {"data": cell}


class FieldValue(BaseModel):
    value: str


class IncidentAttributesPatch(BaseModel):
    fields: dict[str, FieldValue] = {}


class IncidentDataPatch(BaseModel):
    attributes: IncidentAttributesPatch


class IncidentPatch(BaseModel):
    data: IncidentDataPatch


@app.patch("/api/v2/incidents/{incident_id}", dependencies=[Depends(authorize)])
async def patch_incident(incident_id: str, req: IncidentPatch) -> dict[str, Any]:
    inc = incident_or_404(incident_id)
    for name, field in req.data.attributes.fields.items():
        if len(field.value) > 4000:
            raise HTTPException(status_code=400, detail=f"field {name} too long")
        inc["attributes"]["fields"][name] = {"type": "textbox", "value": field.value}
    return {"data": inc}


@app.post("/demo/reset", dependencies=[Depends(authorize)])
async def demo_reset() -> dict[str, Any]:
    """Not part of the real API: puts the incident back to its fixture state between runs."""
    reset()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, access_log=False)
