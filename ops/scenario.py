"""The incident scenario behind the internal ops stand-in: one fictional company, one bad afternoon.

Halyard runs a checkout for online merchants. At 14:02 UTC `ledger-svc` v3.41.0 ships with write
batching and a smaller per-pod database pool (40 -> 8). The change assumed the replica bump in
INFRA-77 would land first; it did not, so the service's total pool drops from 320 to 64
connections. Ledger writes queue for connections, checkout-api times out waiting on the ledger,
and the p99 monitor opens incident #4127 at 14:09.

Two things look suspicious and are not the cause: the `checkout.new-tax-engine` flag ramped from
10% to 50% at 13:55, and a `web-frontend` deploy went out at 13:40. Every data source here agrees
with that story, so an investigation can confirm the cause and rule the others out.

All data is deterministic: metrics and logs are functions of (series, minute), and the warehouse
is generated from a fixed seed.
"""

from __future__ import annotations

import math
import random
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

DAY = datetime(2026, 10, 13, tzinfo=UTC)
DATA_START = DAY.replace(hour=12)
# The moment the agent is paged. Nothing has been recorded after it.
NOW = DAY.replace(hour=14, minute=14)


def at(hhmm: str) -> datetime:
    h, m = hhmm.split(":")
    return DAY.replace(hour=int(h), minute=int(m))


FLAG_RAMP = at("13:55")
LEDGER_DEPLOY = at("14:02")
# Pool exhaustion needs a minute or two of traffic on the new pods before waiters pile up.
DEGRADED = at("14:04")
MONITOR_FIRED = at("14:09")

COMPANY = "Halyard"

SERVICES: dict[str, dict] = {
    "checkout-api": {
        "description": "Public checkout API: carts, payment authorization, order creation.",
        "team": "checkout",
        "on_call": "@maya.lindqvist",
        "tier": 0,
        "runtime": "kubernetes / 24 replicas",
        "slo": "99.9% of POST /v1/checkout under 1500 ms (30d)",
        "depends_on": ["ledger-svc", "tax-engine", "fraud-score", "inventory-svc"],
        "runbooks": ["RB-12"],
        "repo": "halyard/checkout-api",
    },
    "ledger-svc": {
        "description": "Double-entry ledger. Every successful checkout writes its journal entries here, synchronously.",
        "team": "payments-core",
        "on_call": "@dana.okafor",
        "tier": 0,
        "runtime": "kubernetes / 8 replicas (HPA min 8, max 8)",
        "slo": "99.95% of writes under 300 ms (30d)",
        "depends_on": ["payments-db"],
        "runbooks": ["RB-31"],
        "repo": "halyard/ledger",
        "config": {"LEDGER_DB_POOL_MAX": "8", "LEDGER_DB_POOL_TIMEOUT_MS": "1500", "LEDGER_WRITE_BATCH": "25"},
    },
    "tax-engine": {
        "description": "Sales tax and VAT calculation. v2 model behind the checkout.new-tax-engine flag.",
        "team": "commerce-platform",
        "on_call": "@ravi.anand",
        "tier": 1,
        "runtime": "kubernetes / 12 replicas",
        "slo": "99.9% under 250 ms (30d)",
        "depends_on": [],
        "runbooks": [],
        "repo": "halyard/tax-engine",
    },
    "fraud-score": {
        "description": "Real-time fraud scoring for payment attempts.",
        "team": "risk",
        "on_call": "@sofia.reyes",
        "tier": 0,
        "runtime": "kubernetes / 16 replicas",
        "slo": "99.9% under 200 ms (30d)",
        "depends_on": [],
        "runbooks": [],
        "repo": "halyard/fraud-score",
    },
    "inventory-svc": {
        "description": "Stock reservations for cart items.",
        "team": "catalog",
        "on_call": "@tomas.hale",
        "tier": 1,
        "runtime": "kubernetes / 6 replicas",
        "slo": "99.9% under 150 ms (30d)",
        "depends_on": [],
        "runbooks": [],
        "repo": "halyard/inventory",
    },
    "payments-db": {
        "description": "Postgres 16 primary for ledger and payments. max_connections=400; ledger-svc budget is 320.",
        "team": "payments-core",
        "on_call": "@dana.okafor",
        "tier": 0,
        "runtime": "managed postgres, db.r7g.4xlarge",
        "slo": "-",
        "depends_on": [],
        "runbooks": ["RB-31"],
        "repo": "-",
    },
    "web-frontend": {
        "description": "Merchant dashboard and hosted checkout pages.",
        "team": "web",
        "on_call": "@lena.brandt",
        "tier": 2,
        "runtime": "edge / static",
        "slo": "-",
        "depends_on": ["checkout-api"],
        "runbooks": [],
        "repo": "halyard/web",
    },
}

DEPLOYS = [
    {
        "id": "dep-88213",
        "service": "web-frontend",
        "version": "v812",
        "at": at("13:40"),
        "author": "@lena.brandt",
        "change": "WEB-904",
        "title": "Dashboard: new payouts table styles",
        "status": "succeeded",
    },
    {
        "id": "dep-88227",
        "service": "ledger-svc",
        "version": "v3.41.0",
        "previous_version": "v3.40.2",
        "at": LEDGER_DEPLOY,
        "author": "@jonas.berg",
        "change": "LDG-1182",
        "title": "Batch ledger writes; size the DB pool per pod",
        "status": "succeeded",
    },
    {
        "id": "dep-88231",
        "service": "search-svc",
        "version": "v2.9.4",
        "at": at("14:10"),
        "author": "@pia.kowalski",
        "change": "SRCH-310",
        "title": "Reindex merchant catalog nightly instead of hourly",
        "status": "succeeded",
    },
    {
        "id": "dep-88140",
        "service": "checkout-api",
        "version": "v5.18.1",
        "at": at("11:22"),
        "author": "@maya.lindqvist",
        "change": "CHK-2210",
        "title": "Log idempotency-key collisions at INFO",
        "status": "succeeded",
    },
]

CHANGES = {
    "LDG-1182": {
        "title": "Batch ledger writes; size the DB pool per pod",
        "author": "@jonas.berg",
        "reviewers": ["@dana.okafor"],
        "merged_at": at("13:31"),
        "description": (
            "Writes journal entries in batches of up to 25 instead of one statement per entry. With "
            "batching each pod needs far fewer connections, so this sizes the pool per pod instead of "
            "for the old fleet: LEDGER_DB_POOL_MAX 40 -> 8. Total connections stay within the "
            "payments-db budget once ledger-svc scales to 32 replicas (INFRA-77)."
        ),
        "diff": [
            "config/production.env",
            "-LEDGER_DB_POOL_MAX=40",
            "+LEDGER_DB_POOL_MAX=8",
            "-LEDGER_WRITE_BATCH=1",
            "+LEDGER_WRITE_BATCH=25",
            "src/ledger/writer.go  +118 -41  (batch flush every 25 entries or 20 ms)",
        ],
        "linked": ["INFRA-77"],
    },
    "INFRA-77": {
        "title": "Scale ledger-svc to 32 replicas",
        "author": "@jonas.berg",
        "reviewers": [],
        "merged_at": None,
        "description": "Raise ledger-svc HPA min/max from 8 to 32. Blocked on node pool quota (open).",
        "diff": ["deploy/ledger-svc/hpa.yaml", "-  minReplicas: 8", "+  minReplicas: 32"],
        "linked": ["LDG-1182"],
        "state": "open",
    },
    "WEB-904": {
        "title": "Dashboard: new payouts table styles",
        "author": "@lena.brandt",
        "reviewers": ["@tomas.hale"],
        "merged_at": at("13:22"),
        "description": "CSS-only change to the merchant dashboard payouts table.",
        "diff": ["src/dashboard/payouts.css  +64 -12"],
        "linked": [],
    },
    "SRCH-310": {
        "title": "Reindex merchant catalog nightly instead of hourly",
        "author": "@pia.kowalski",
        "reviewers": ["@tomas.hale"],
        "merged_at": at("13:58"),
        "description": "Moves the catalog reindex cron. search-svc is not on the checkout path.",
        "diff": ["cron/reindex.yaml  -  schedule: '0 * * * *'  +  schedule: '0 3 * * *'"],
        "linked": [],
    },
    "CHK-2210": {
        "title": "Log idempotency-key collisions at INFO",
        "author": "@maya.lindqvist",
        "reviewers": ["@ravi.anand"],
        "merged_at": at("11:05"),
        "description": "Log level change only.",
        "diff": ["src/checkout/idempotency.go  +2 -2"],
        "linked": [],
    },
}

FLAG_EVENTS = [
    {
        "flag": "checkout.new-tax-engine",
        "service": "checkout-api",
        "at": at("09:30"),
        "change": "rollout 0% -> 10%",
        "by": "@ravi.anand",
    },
    {
        "flag": "checkout.new-tax-engine",
        "service": "checkout-api",
        "at": FLAG_RAMP,
        "change": "rollout 10% -> 50%",
        "by": "@ravi.anand",
    },
    {
        "flag": "dashboard.payouts-v2",
        "service": "web-frontend",
        "at": at("13:41"),
        "change": "enabled for all merchants",
        "by": "@lena.brandt",
    },
]

RUNBOOKS = {
    "RB-12": {
        "title": "checkout-api: latency or error-rate alert",
        "owner": "checkout",
        "markdown": """# checkout-api latency / error-rate alert

1. Find the slow hop. `trace_breakdown` for checkout-api shows each downstream's share of latency.
   checkout-api itself rarely regresses without a deploy of its own.
2. List deploys and flag changes for checkout-api **and its tier-0 dependencies** in the 60 minutes
   before the alert. Read the change behind any candidate.
3. Confirm with the owning service's metrics and logs before blaming it.
4. Quantify customer impact from the warehouse (`checkout_attempts`): failed attempts and the
   GMV they represent, from first symptom to now.
5. Mitigate with the fastest safe action. Rolling back a dependency needs the owning team's on-call
   to approve the change request (`request_rollback`); never roll back without it.
6. Post findings to the incident timeline.""",
    },
    "RB-31": {
        "title": "ledger-svc / payments-db: connection pool exhaustion",
        "owner": "payments-core",
        "markdown": """# Connection pool exhaustion (ledger-svc)

Symptoms: `db.pool.wait_ms_p99` high, `db.pool.in_use` pinned at `db.pool.max`, log line
`could not acquire connection from pool`.

The ledger's total pool is `LEDGER_DB_POOL_MAX x replicas`. It must stay at or under its
payments-db budget (320) and should not be far below it: the service is sized for ~300
concurrent writes at peak.

Mitigation: roll back to the last version with a known-good pool size, or raise
`LEDGER_DB_POOL_MAX`. Either needs payments-core on-call approval.""",
    },
}

TRACE_BREAKDOWN = {
    # (downstream, normal p99 ms, degraded p99 ms, normal error %, degraded error %)
    "checkout-api": [
        ("ledger-svc", 180, 3900, 0.0, 8.7),
        ("fraud-score", 120, 124, 0.0, 0.0),
        ("tax-engine", 58, 61, 0.0, 0.0),
        ("inventory-svc", 35, 36, 0.0, 0.0),
    ],
    "ledger-svc": [
        ("payments-db (query)", 22, 24, 0.0, 0.0),
        ("pool.acquire", 2, 1500, 0.0, 8.9),
    ],
}


# ---- Metrics ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    unit: str
    description: str
    value: Callable[[datetime], float]


def _noise(series: str, t: datetime, scale: float) -> float:
    rng = random.Random(zlib.crc32(f"{series}{t.isoformat()}".encode()))
    return (rng.random() - 0.5) * 2 * scale


def _ramp(t: datetime, start: datetime, minutes: float) -> float:
    """0 before `start`, rising to 1 over `minutes`."""
    if t < start:
        return 0.0
    return min(1.0, (t - start).total_seconds() / 60 / minutes)


def _traffic(t: datetime) -> float:
    """Afternoon traffic: gently rising toward the 15:00 peak."""
    minutes = (t - DATA_START).total_seconds() / 60
    return 1.0 + 0.15 * math.sin(minutes / 90)


def _bad(t: datetime) -> float:
    return _ramp(t, DEGRADED, 3)


def _new_pods(t: datetime) -> float:
    return _ramp(t, LEDGER_DEPLOY, 2)


METRICS: dict[str, dict[str, Metric]] = {
    "checkout-api": {
        "http.p99_ms": Metric(
            "ms",
            "p99 latency of POST /v1/checkout",
            lambda t: 380 * _traffic(t) + 3720 * _bad(t) + _noise("chk.p99", t, 40 + 300 * _bad(t)),
        ),
        "http.error_rate_pct": Metric(
            "%",
            "share of checkout requests answered 5xx",
            lambda t: max(0.0, 0.3 + 8.6 * _bad(t) + _noise("chk.err", t, 0.1 + 0.8 * _bad(t))),
        ),
        "http.rps": Metric(
            "req/s", "checkout requests per second", lambda t: 118 * _traffic(t) + _noise("chk.rps", t, 6)
        ),
    },
    "ledger-svc": {
        "http.p99_ms": Metric(
            "ms",
            "p99 latency of journal writes",
            lambda t: 180 + 3720 * _bad(t) + _noise("ldg.p99", t, 15 + 250 * _bad(t)),
        ),
        "db.pool.max": Metric(
            "connections",
            "configured pool size, summed over replicas",
            lambda t: 320 - 256 * _new_pods(t),
        ),
        "db.pool.in_use": Metric(
            "connections",
            "connections checked out, summed over replicas",
            lambda t: min(320 - 256 * _new_pods(t), 210 * _traffic(t) + _noise("ldg.inuse", t, 12)),
        ),
        "db.pool.wait_ms_p99": Metric(
            "ms",
            "p99 time a write waits to acquire a connection",
            lambda t: max(0.5, 2 + 1498 * _bad(t) + _noise("ldg.wait", t, 0.5 + 30 * _bad(t))),
        ),
        "replicas": Metric("pods", "ready replicas", lambda t: 8.0),
    },
    "tax-engine": {
        "http.p99_ms": Metric("ms", "p99 latency of /v1/quote", lambda t: 58 + _noise("tax.p99", t, 4)),
        "http.error_rate_pct": Metric(
            "%", "share of requests answered 5xx", lambda t: max(0.0, 0.02 + _noise("tax.err", t, 0.02))
        ),
    },
    "fraud-score": {
        "http.p99_ms": Metric("ms", "p99 latency of /v1/score", lambda t: 120 + _noise("frd.p99", t, 8)),
    },
    "payments-db": {
        "cpu_pct": Metric("%", "primary CPU", lambda t: 38 - 9 * _bad(t) + _noise("db.cpu", t, 3)),
        "connections.active": Metric(
            "connections",
            "active client connections, all clients",
            lambda t: 252 - 186 * _new_pods(t) + _noise("db.conn", t, 6),
        ),
        "locks.waiting": Metric("locks", "queries waiting on a lock", lambda t: max(0.0, 1 + _noise("db.lock", t, 1))),
    },
}


# ---- Logs ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LogPattern:
    level: str
    template: str
    per_minute: Callable[[datetime], float]


LOGS: dict[str, list[LogPattern]] = {
    "checkout-api": [
        LogPattern(
            "ERROR",
            "upstream ledger-svc timed out after 2000ms (POST /v1/journal) order_id={order} attempt=1",
            lambda t: 610 * _bad(t),
        ),
        LogPattern(
            "WARN",
            "checkout failed, returning 503 to client: ledger write not confirmed order_id={order}",
            lambda t: 590 * _bad(t),
        ),
        LogPattern(
            "INFO",
            "tax quote via tax-engine model=v2 in {ms}ms order_id={order}",
            lambda t: 7080 * _traffic(t) * (0.5 if t >= FLAG_RAMP else 0.1),
        ),
        LogPattern(
            "INFO",
            "idempotency-key collision, replaying stored response key={key}",
            lambda t: 14.0,
        ),
    ],
    "ledger-svc": [
        LogPattern(
            "ERROR",
            "could not acquire connection from pool within 1500ms (pool_max=8 in_use=8 waiters={waiters})",
            lambda t: 640 * _bad(t),
        ),
        LogPattern(
            "INFO",
            "flushed journal batch size={batch} in {ms}ms",
            lambda t: 300 * _traffic(t) * _new_pods(t),
        ),
        LogPattern(
            "INFO",
            "starting ledger-svc v3.41.0 pool_max=8 write_batch=25",
            lambda t: 8.0 if LEDGER_DEPLOY <= t < LEDGER_DEPLOY + timedelta(minutes=1) else 0.0,
        ),
    ],
    "tax-engine": [
        LogPattern(
            "INFO",
            "quote model=v2 region={region} in {ms}ms",
            lambda t: 3540 * _traffic(t) * (0.5 if t >= FLAG_RAMP else 0.1),
        ),
    ],
    "payments-db": [
        LogPattern("LOG", "checkpoint complete: wrote {n} buffers", lambda t: 0.2),
        LogPattern(
            "LOG",
            "disconnection: session time {dur} user=ledger database=payments",
            lambda t: 256.0 if LEDGER_DEPLOY <= t < LEDGER_DEPLOY + timedelta(minutes=2) else 0.0,
        ),
    ],
    "web-frontend": [],
    "fraud-score": [],
    "inventory-svc": [],
}


def fill(template: str, series: str, t: datetime, i: int) -> str:
    rng = random.Random(zlib.crc32(f"{series}{t.isoformat()}{i}".encode()))
    values = {
        "order": f"ord_{rng.randrange(16**8):08x}",
        "key": f"idem_{rng.randrange(16**6):06x}",
        "ms": str(rng.randint(30, 70)),
        "waiters": str(rng.randint(40, 190)),
        "batch": str(rng.randint(9, 25)),
        "region": rng.choice(["us-ca", "us-ny", "de", "gb", "fr"]),
        "n": str(rng.randint(800, 2400)),
        "dur": f"0:{rng.randint(10, 59)}:{rng.randint(10, 59)}.{rng.randint(100, 999)}",
    }
    return template.format(**values)


# ---- Warehouse -------------------------------------------------------------------------------

# One row per checkout attempt; ~120 a minute is a 1/60 sample of real traffic.
ATTEMPTS_PER_MINUTE = 120
CARD_DECLINE_RATE = 0.021


def checkout_attempts() -> list[dict]:
    rng = random.Random(4127)
    rows = []
    t = DATA_START
    n = 0
    while t < NOW:
        for _ in range(int(ATTEMPTS_PER_MINUTE * _traffic(t))):
            n += 1
            ts = t + timedelta(seconds=rng.random() * 60)
            cohort = "v2" if rng.random() < (0.5 if ts >= FLAG_RAMP else 0.1) else "v1"
            amount = round(math.exp(rng.gauss(4.1, 0.8)), 2)
            if rng.random() < 0.087 * _bad(ts):
                status, error, latency = "failed", "LEDGER_TIMEOUT", rng.randint(2000, 2100)
            elif rng.random() < CARD_DECLINE_RATE:
                status, error, latency = "failed", "CARD_DECLINED", rng.randint(250, 700)
            else:
                status, error, latency = "succeeded", None, int(rng.gauss(310, 60) + 3000 * _bad(ts) * rng.random())
            rows.append(
                {
                    "attempted_at": ts,
                    "attempt_id": f"att_{n:06d}",
                    "merchant_id": f"m_{rng.randint(1, 900):04d}",
                    "region": rng.choice(["us-east", "us-west", "eu-west", "eu-central"]),
                    "payment_method": rng.choice(["card", "card", "card", "wallet", "bank_transfer"]),
                    "amount_usd": amount,
                    "status": status,
                    "error_code": error,
                    "latency_ms": max(80, latency),
                    "tax_engine_cohort": cohort,
                }
            )
        t += timedelta(minutes=1)
    return rows
