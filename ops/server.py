"""Halyard's internal ops MCP server: the stand-in for an internal data platform an SRE agent needs.

A real company would put its metrics store, log search, tracing, deploy history, feature-flag
audit log, runbooks and warehouse behind one MCP server like this one. Here every answer comes
from scenario.py, so the data is fictional but internally consistent.

Served over MCP's streamable HTTP transport at /mcp. Every request must carry
`Authorization: Bearer $OPS_MCP_TOKEN`, and every tool call is logged as one JSON line.
All tools are read-only except `request_rollback`, which only opens a change request: a human
on the owning team must approve it before anything is rolled back.
"""

from __future__ import annotations

import csv
import hmac
import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import scenario as s

TOKEN = os.environ.get("OPS_MCP_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_POINTS = 60
SQL_ROW_LIMIT = 50

logging.basicConfig(level=logging.INFO, format="%(message)s")
audit = logging.getLogger("audit")

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

mcp = MCPServer(
    name="halyard-ops",
    title="Halyard internal ops",
    instructions=(
        f"Internal observability and change data for {s.COMPANY}. All times are UTC ISO-8601. "
        f"Data starts at {s.DATA_START.isoformat()} and ends now ({s.NOW.isoformat()})."
    ),
)


def parse_time(value: str, default: datetime) -> datetime:
    """Accepts ISO-8601, 'now', or 'now-30m' / 'now-2h'. Clamped to the recorded window."""
    value = (value or "").strip()
    if not value:
        t = default
    elif value.startswith("now"):
        m = re.fullmatch(r"now(?:-(\d+)([mh]))?", value)
        if m is None:
            raise ToolError(f"cannot parse time {value!r}")
        t = s.NOW
        if m.group(1):
            t -= timedelta(minutes=int(m.group(1)) * (60 if m.group(2) == "h" else 1))
    else:
        try:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise ToolError(f"cannot parse time {value!r}: use ISO-8601 or 'now-30m'") from e
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
    return min(max(t, s.DATA_START), s.NOW)


def window(start: str, end: str) -> tuple[datetime, datetime]:
    t0 = parse_time(start, s.NOW - timedelta(hours=1))
    t1 = parse_time(end, s.NOW)
    if t1 <= t0:
        raise ToolError("end must be after start")
    return t0, t1


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def known_service(service: str) -> dict[str, Any]:
    if service not in s.SERVICES:
        raise ToolError(f"unknown service {service!r}; known: {', '.join(s.SERVICES)}")
    return s.SERVICES[service]


@mcp.tool(annotations=READ_ONLY)
def service_catalog(service: str) -> dict[str, Any]:
    """Look up a service: owning team, on-call, tier, SLO, dependencies, current config and runbooks."""
    entry = known_service(service)
    dependents = [name for name, e in s.SERVICES.items() if service in e["depends_on"]]
    return {"service": service, **entry, "depended_on_by": dependents}


@mcp.tool(annotations=READ_ONLY)
def list_metrics(service: str) -> dict[str, Any]:
    """List the metric names recorded for a service, with units and descriptions."""
    known_service(service)
    return {
        "service": service,
        "metrics": [
            {"name": name, "unit": m.unit, "description": m.description}
            for name, m in s.METRICS.get(service, {}).items()
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def query_metrics(service: str, metric: str, start: str = "now-1h", end: str = "now") -> dict[str, Any]:
    """Read one metric as a time series. Times are ISO-8601 or 'now-30m' style. Returns at most 60
    points (averaged into wider buckets for long windows) plus min / max / last."""
    known_service(service)
    m = s.METRICS.get(service, {}).get(metric)
    if m is None:
        raise ToolError(f"no metric {metric!r} for {service}; call list_metrics")
    t0, t1 = window(start, end)
    minutes = int((t1 - t0).total_seconds() // 60)
    step = max(1, -(-minutes // MAX_POINTS))
    points = []
    for i in range(0, minutes, step):
        bucket = [t0 + timedelta(minutes=i + j) for j in range(step) if i + j < minutes]
        points.append([iso(bucket[0]), round(sum(m.value(t) for t in bucket) / len(bucket), 2)])
    values = [p[1] for p in points]
    return {
        "service": service,
        "metric": metric,
        "unit": m.unit,
        "step_minutes": step,
        "points": points,
        "min": min(values),
        "max": max(values),
        "last": values[-1],
    }


@mcp.tool(annotations=READ_ONLY)
def search_logs(
    service: str, query: str = "", level: str = "", start: str = "now-30m", end: str = "now"
) -> dict[str, Any]:
    """Search a service's logs. Returns matching log patterns ranked by count, with when each first
    and last appeared and a few sample lines. `query` is a case-insensitive substring; `level` is
    one of ERROR, WARN, INFO or empty for all."""
    known_service(service)
    t0, t1 = window(start, end)
    patterns = []
    for p in s.LOGS.get(service, []):
        if level and p.level != level.upper():
            continue
        if query and query.lower() not in p.template.lower():
            continue
        count, first, last, recent = 0, None, None, []
        t = t0
        while t < t1:
            n = int(round(p.per_minute(t)))
            if n:
                count += n
                first = first or t
                last = t
                recent = (recent + [t])[-3:]
            t += timedelta(minutes=1)
        samples = [f"{iso(t)} {p.level} {s.fill(p.template, service, t, i)}" for i, t in enumerate(recent)]
        if count:
            patterns.append(
                {
                    "level": p.level,
                    "pattern": re.sub(r"\{\w+\}", "*", p.template),
                    "count": count,
                    "first_seen": iso(first),
                    "last_seen": iso(last),
                    "samples": samples,
                }
            )
    patterns.sort(key=lambda p: -p["count"])
    return {"service": service, "start": iso(t0), "end": iso(t1), "patterns": patterns}


@mcp.tool(annotations=READ_ONLY)
def trace_breakdown(service: str, start: str = "now-15m", end: str = "now") -> dict[str, Any]:
    """Where a service's request time goes: p99 latency and error rate of each downstream call it
    makes, from distributed traces."""
    known_service(service)
    rows = s.TRACE_BREAKDOWN.get(service)
    if rows is None:
        return {"service": service, "downstream": [], "note": "no outbound spans recorded"}
    t0, t1 = window(start, end)
    mid = t0 + (t1 - t0) / 2
    bad = 1.0 if mid >= s.DEGRADED else 0.0
    return {
        "service": service,
        "start": iso(t0),
        "end": iso(t1),
        "downstream": [
            {
                "call": name,
                "p99_ms": round(p99 + (p99_bad - p99) * bad),
                "error_pct": round(err + (err_bad - err) * bad, 1),
            }
            for name, p99, p99_bad, err, err_bad in rows
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def list_deploys(start: str = "now-2h", end: str = "now", service: str = "") -> dict[str, Any]:
    """Production deploys in a window, newest first, across all services or for one."""
    t0, t1 = window(start, end)
    deploys = [
        {**{k: v for k, v in d.items() if k != "at"}, "at": iso(d["at"])}
        for d in sorted(s.DEPLOYS, key=lambda d: d["at"], reverse=True)
        if t0 <= d["at"] <= t1 and (not service or d["service"] == service)
    ]
    return {"start": iso(t0), "end": iso(t1), "deploys": deploys}


@mcp.tool(annotations=READ_ONLY)
def get_change(change_id: str) -> dict[str, Any]:
    """Read a change request (pull request / ticket): description, reviewers, diff summary, links."""
    change = s.CHANGES.get(change_id.upper())
    if change is None:
        raise ToolError(f"no change {change_id}")
    return {"id": change_id.upper(), **change, "merged_at": iso(change["merged_at"])}


@mcp.tool(annotations=READ_ONLY)
def feature_flag_history(start: str = "now-2h", end: str = "now", service: str = "") -> dict[str, Any]:
    """Feature-flag changes in a window, newest first."""
    t0, t1 = window(start, end)
    events = [
        {**{k: v for k, v in e.items() if k != "at"}, "at": iso(e["at"])}
        for e in sorted(s.FLAG_EVENTS, key=lambda e: e["at"], reverse=True)
        if t0 <= e["at"] <= t1 and (not service or e["service"] == service)
    ]
    return {"start": iso(t0), "end": iso(t1), "events": events}


@mcp.tool(annotations=READ_ONLY)
def get_runbook(runbook_id: str) -> dict[str, Any]:
    """Read a runbook as markdown."""
    rb = s.RUNBOOKS.get(runbook_id.upper())
    if rb is None:
        raise ToolError(f"no runbook {runbook_id}; known: {', '.join(s.RUNBOOKS)}")
    return {"id": runbook_id.upper(), **rb}


def build_warehouse() -> duckdb.DuckDBPyConnection:
    rows = s.checkout_attempts()
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({**r, "attempted_at": iso(r["attempted_at"])} for r in rows)
        f.flush()
        con.execute(
            "CREATE TABLE checkout_attempts AS SELECT * FROM read_csv(?, header = true, columns = {"
            + "'attempted_at': 'TIMESTAMPTZ', 'attempt_id': 'VARCHAR', 'merchant_id': 'VARCHAR', "
            + "'region': 'VARCHAR', 'payment_method': 'VARCHAR', 'amount_usd': 'DOUBLE', "
            + "'status': 'VARCHAR', 'error_code': 'VARCHAR', 'latency_ms': 'INTEGER', "
            + "'tax_engine_cohort': 'VARCHAR'})",
            [f.name],
        )
    # From here on, queries may read the table and nothing else: no files, no network, no settings.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


WAREHOUSE = build_warehouse()


@mcp.tool(annotations=READ_ONLY)
def query_warehouse(sql: str) -> dict[str, Any]:
    """Run one read-only SQL query (DuckDB dialect) against the analytics warehouse.

    Table checkout_attempts, one row per checkout attempt, sampled 1 in 60:
      attempted_at TIMESTAMPTZ, attempt_id, merchant_id, region, payment_method,
      amount_usd DOUBLE, status ('succeeded' | 'failed'), error_code (NULL | 'CARD_DECLINED' |
      'LEDGER_TIMEOUT'), latency_ms INTEGER, tax_engine_cohort ('v1' | 'v2').
    Multiply counts and amounts by 60 to estimate totals. Returns at most 50 rows."""
    statement = sql.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", statement) or ";" in statement:
        raise ToolError("only a single SELECT (or WITH ... SELECT) statement is allowed")
    cur = WAREHOUSE.cursor()
    try:
        cur.execute(statement)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(SQL_ROW_LIMIT + 1)
    except duckdb.Error as e:
        raise ToolError(f"{type(e).__name__}: {e}") from e
    out = [{c: (iso(v) if isinstance(v, datetime) else v) for c, v in zip(columns, r)} for r in rows[:SQL_ROW_LIMIT]]
    return {"columns": columns, "rows": out, "truncated": len(rows) > SQL_ROW_LIMIT}


CHANGE_REQUESTS: list[dict[str, Any]] = []


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def request_rollback(service: str, to_version: str, reason: str, incident: str) -> dict[str, Any]:
    """Open a rollback change request. This does not roll anything back: the owning team's on-call
    must approve it, and the deploy system runs it only after approval."""
    entry = known_service(service)
    request = {
        "id": f"CR-{5521 + len(CHANGE_REQUESTS)}",
        "service": service,
        "to_version": to_version,
        "reason": reason,
        "incident": incident,
        "status": "pending_approval",
        "approver": entry["on_call"],
        "opened_at": iso(s.NOW),
    }
    CHANGE_REQUESTS.append(request)
    return request


class BearerAuthAndAudit:
    """Rejects requests without the bearer token, and logs every MCP request as one JSON line."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__()
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        supplied = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
        ok = bool(TOKEN) and hmac.compare_digest(supplied, TOKEN)

        body = b""

        async def recording_receive() -> dict[str, Any]:
            nonlocal body
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
            return message

        if not ok:
            audit.info(json.dumps({"ts": round(time.time(), 3), "path": scope["path"], "authorized": False}))
            await JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, recording_receive, send)
        try:
            msg = json.loads(body or b"{}")
        except json.JSONDecodeError:
            msg = {}
        if isinstance(msg, dict) and msg.get("method"):
            params = msg.get("params") or {}
            audit.info(
                json.dumps(
                    {
                        "ts": round(time.time(), 3),
                        "method": msg["method"],
                        "tool": params.get("name"),
                        "arguments": params.get("arguments"),
                        "authorized": True,
                    }
                )
            )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "tools": len(await mcp.list_tools())})


def build_app() -> ASGIApp:
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # The gateway terminates TLS and rewrites Host, and every request is authenticated, so the
        # SDK's localhost-only DNS-rebinding guard has nothing to protect here.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    return BearerAuthAndAudit(app)


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, access_log=False)
