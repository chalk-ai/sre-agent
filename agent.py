"""An SRE agent that investigates a Datadog incident with the company's internal ops MCP server.

Runs inside a Chalk sandbox started by run.py (or by the webhook responder). It gets:
  - the incident webhook payload as its only argument;
  - Datadog tools for reading the incident and its monitor and for writing notes back;
  - every tool the internal ops MCP server advertises, discovered at startup over MCP;
  - a model, through the Chalk model router.

Every credential it holds is a placeholder that the sandbox's egress proxy swaps for the real
value, and only on requests to the host that credential belongs to.

Prints one JSON object per line: the run's trace, which run.py renders and records.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

CHALK_API = os.environ["CHALK_API_HOST"].rstrip("/")
ENV_ID = os.environ["CHALK_ENVIRONMENT_ID"]
OPS_MCP_URL = os.environ["OPS_MCP_URL"]
DD_API = os.environ["DD_API_URL"].rstrip("/")
MODEL = os.environ["AGENT_MODEL"]
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "30"))
MAX_RESULT_CHARS = 6000

OPS_SERVER = "halyard-ops"
DATADOG = "datadog"


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def compact(text: str) -> str:
    """Tool results arrive as indented JSON; re-serialize compactly and cap the size."""
    try:
        text = json.dumps(json.loads(text), separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        pass
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"... [truncated {len(text) - MAX_RESULT_CHARS} chars]"
    return text


# ---- Datadog --------------------------------------------------------------------------------


class Datadog:
    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__()
        self.http = http
        self.headers = {"DD-API-KEY": os.environ["DD_API_KEY"], "DD-APPLICATION-KEY": os.environ["DD_APP_KEY"]}

    async def request(self, method: str, path: str, body: dict[str, Any] | None) -> str:
        resp = await self.http.request(method, f"{DD_API}{path}", headers=self.headers, json=body)
        if resp.status_code >= 300:
            raise RuntimeError(f"Datadog API {resp.status_code}: {resp.text[:500]}")
        return resp.text

    async def get_incident(self, incident_id: str) -> str:
        return await self.request("GET", f"/api/v2/incidents/{incident_id}", None)

    async def get_monitor(self, monitor_id: int) -> str:
        return await self.request("GET", f"/api/v1/monitor/{monitor_id}", None)

    async def add_timeline_note(self, incident_id: str, markdown: str) -> str:
        body = {"data": {"attributes": {"cell_type": "markdown", "content": {"content": markdown}}}}
        return await self.request("POST", f"/api/v2/incidents/{incident_id}/timeline", body)

    async def set_root_cause(self, incident_id: str, root_cause: str) -> str:
        body = {"data": {"attributes": {"fields": {"root_cause": {"value": root_cause}}}}}
        return await self.request("PATCH", f"/api/v2/incidents/{incident_id}", body)


def fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


DATADOG_TOOLS = [
    fn("datadog_get_incident", "Read a Datadog incident by id.", {"incident_id": {"type": "string"}}, ["incident_id"]),
    fn(
        "datadog_get_monitor",
        "Read the Datadog monitor that opened the incident: its query, thresholds and message.",
        {"monitor_id": {"type": "integer"}},
        ["monitor_id"],
    ),
    fn(
        "datadog_add_timeline_note",
        "Post a short markdown note to the incident timeline, visible to every responder. Use it to say "
        + "what you are checking and, once you have one, the root cause. Keep notes brief.",
        {"incident_id": {"type": "string"}, "markdown": {"type": "string"}},
        ["incident_id", "markdown"],
    ),
]

SUBMIT_FINDINGS = fn(
    "submit_findings",
    "Finish the investigation. Call exactly once, at the end. This posts your findings to the incident "
    + "timeline and records the root cause on the incident.",
    {
        "summary": {"type": "string", "description": "Two or three sentences for the incident channel."},
        "root_cause": {"type": "string", "description": "One sentence."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"finding": {"type": "string"}, "source": {"type": "string"}},
                "required": ["finding", "source"],
            },
        },
        "ruled_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"hypothesis": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["hypothesis", "reason"],
            },
        },
        "impact": {
            "type": "object",
            "properties": {
                "started_at": {"type": "string"},
                "failed_checkouts_estimate": {"type": "integer"},
                "gmv_at_risk_usd": {"type": "number"},
                "notes": {"type": "string"},
            },
            "required": ["started_at", "failed_checkouts_estimate", "gmv_at_risk_usd"],
        },
        "mitigation": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "change_request": {"type": "string"},
                "status": {"type": "string"},
                "approver": {"type": "string"},
            },
            "required": ["action", "status"],
        },
        "follow_ups": {"type": "array", "items": {"type": "string"}},
    },
    ["summary", "root_cause", "confidence", "evidence", "ruled_out", "impact", "mitigation", "follow_ups"],
)


def findings_markdown(f: dict[str, Any]) -> str:
    impact = f["impact"]
    mitigation = f["mitigation"]
    lines = [
        f"**SRE agent findings** (confidence: {f['confidence']})",
        "",
        f["summary"],
        "",
        f"**Root cause:** {f['root_cause']}",
        "",
        "**Evidence**",
        *[f"- {e['finding']} _({e['source']})_" for e in f["evidence"]],
        "",
        "**Ruled out**",
        *[f"- {r['hypothesis']}: {r['reason']}" for r in f["ruled_out"]],
        "",
        f"**Impact** since {impact['started_at']}: ~{impact['failed_checkouts_estimate']:,} failed checkouts, "
        + f"~${impact['gmv_at_risk_usd']:,.0f} GMV at risk. {impact.get('notes', '')}".rstrip(),
        "",
        f"**Mitigation:** {mitigation['action']} ({mitigation['status']}"
        + (f", {mitigation['change_request']}" if mitigation.get("change_request") else "")
        + (f", approver {mitigation['approver']}" if mitigation.get("approver") else "")
        + ")",
    ]
    if f.get("follow_ups"):
        lines += ["", "**Follow-ups**", *[f"- {x}" for x in f["follow_ups"]]]
    return "\n".join(lines)


# ---- Model -----------------------------------------------------------------------------------


def system_prompt(tool_count: int) -> str:
    return f"""You are the on-call SRE agent at Halyard, a checkout and payments platform. A Datadog incident
has just opened and paged you. Investigate it the way a senior SRE would, using the {tool_count} tools
on Halyard's internal ops MCP server (service catalog, metrics, logs, traces, deploys, change
requests, feature flags, runbooks and the analytics warehouse) and the Datadog tools.

How to work:
- Start by reading the incident and its monitor, then follow the runbook it names.
- Form hypotheses from what changed recently (deploys and flag changes on the affected service
  and its dependencies) and confirm or reject each one with data from at least two independent
  sources (metrics, logs, traces, warehouse). Do not blame a change on timing alone.
- Explicitly rule out the other recent changes, with the evidence that rules them out.
- Quantify customer impact from the warehouse.
- Post a brief timeline note when you start and another once you know the root cause.
- If you are confident, request the safest mitigation (for example a rollback). You cannot deploy
  anything yourself: a rollback request waits for the owning team's on-call to approve it.
- Be efficient: prefer targeted queries and narrow time windows. Finish by calling submit_findings."""


async def chat(http: httpx.AsyncClient, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    resp = await http.post(
        f"{CHALK_API}/v1/router/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['CHALK_API_TOKEN']}", "X-Chalk-Env-Id": ENV_ID},
        json={"model": MODEL, "messages": messages, "tools": tools, "max_tokens": 4096},
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]


async def investigate(webhook: dict[str, Any]) -> int:
    emit({"type": "incident", "webhook": webhook, "model": MODEL})
    ops_http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {os.environ['OPS_MCP_TOKEN']}"}, timeout=60)
    async with (
        httpx.AsyncClient(timeout=120) as http,
        Client(streamable_http_client(OPS_MCP_URL, http_client=ops_http)) as ops,
    ):
        datadog = Datadog(http)
        mcp_tools = (await ops.list_tools()).tools
        emit(
            {
                "type": "mcp_tools",
                "server": OPS_SERVER,
                "tools": [{"name": t.name, "description": (t.description or "").split("\n")[0]} for t in mcp_tools],
            }
        )
        tools = [
            fn(t.name, t.description or "", t.input_schema.get("properties", {}), t.input_schema.get("required", []))
            for t in mcp_tools
        ]
        tools += DATADOG_TOOLS + [SUBMIT_FINDINGS]
        mcp_names = {t.name for t in mcp_tools}

        async def call(name: str, args: dict[str, Any]) -> tuple[str, bool]:
            if name in mcp_names:
                result = await ops.call_tool(name, args)
                text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
                return text, bool(result.is_error)
            if name == "datadog_get_incident":
                return await datadog.get_incident(args["incident_id"]), False
            if name == "datadog_get_monitor":
                return await datadog.get_monitor(int(args["monitor_id"])), False
            if name == "datadog_add_timeline_note":
                return await datadog.add_timeline_note(args["incident_id"], args["markdown"]), False
            return f"unknown tool {name}", True

        incident_id = str(webhook.get("incident_id") or webhook.get("public_id"))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(len(mcp_tools))},
            {"role": "user", "content": "Datadog incident webhook:\n" + json.dumps(webhook, indent=2)},
        ]
        t_start = time.monotonic()
        for _ in range(MAX_STEPS):
            msg = await chat(http, messages, tools)
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if msg.get("content"):
                emit({"type": "thought", "text": msg["content"]})
            calls = msg.get("tool_calls") or []
            if not calls:
                messages.append({"role": "user", "content": "Continue, or call submit_findings if you are done."})
                continue
            for c in calls:
                name = c["function"]["name"]
                args = json.loads(c["function"]["arguments"] or "{}")
                if name == "submit_findings":
                    note = findings_markdown(args)
                    await datadog.add_timeline_note(incident_id, note)
                    await datadog.set_root_cause(incident_id, args["root_cause"])
                    emit({"type": "datadog_write", "action": "timeline_note", "markdown": note})
                    emit(
                        {
                            "type": "datadog_write",
                            "action": "set_field",
                            "field": "root_cause",
                            "value": args["root_cause"],
                        }
                    )
                    emit({"type": "findings", **args, "elapsed_s": round(time.monotonic() - t_start, 1)})
                    return 0
                source = OPS_SERVER if name in mcp_names else DATADOG
                emit({"type": "tool_call", "id": c["id"], "source": source, "name": name, "args": args})
                t0 = time.monotonic()
                try:
                    text, is_error = await call(name, args)
                except Exception as e:  # The model sees the failure and can recover from it.
                    text, is_error = f"ERROR: {type(e).__name__}: {e}", True
                text = compact(text)
                emit(
                    {
                        "type": "tool_result",
                        "id": c["id"],
                        "name": name,
                        "is_error": is_error,
                        "result": text,
                        "ms": round((time.monotonic() - t0) * 1000),
                    }
                )
                if name == "datadog_add_timeline_note" and not is_error:
                    emit({"type": "datadog_write", "action": "timeline_note", "markdown": args["markdown"]})
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
        emit({"type": "error", "text": f"no findings after {MAX_STEPS} steps"})
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(investigate(json.loads(sys.argv[1]))))
