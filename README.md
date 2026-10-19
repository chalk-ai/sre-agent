# An SRE agent that answers Datadog incidents

A Datadog incident opens. A webhook starts an investigation. Forty seconds later the incident
timeline has a root cause, the evidence behind it, the hypotheses it ruled out, the customer
impact in dollars, and a rollback request waiting for a human to approve it.

The agent investigates the way an on-call engineer would. It reads the incident, follows the
runbook, lists what changed, and then checks each suspect against metrics, logs, traces and the
warehouse. It does that through the company's **internal ops MCP server**, a single MCP endpoint
in front of the metrics store, log search, tracing, deploy history, feature-flag audit log,
runbooks and analytics warehouse. It runs in a **Chalk sandbox** that can reach only three hosts,
using credentials it never sees.

```
Datadog ──webhook──▶ responder (ScalingGroup) ──starts──▶ sandbox: agent.py
                                                            │  model ──▶ Chalk model router
                                                            │  MCP   ──▶ internal ops MCP server
                                                            └  REST  ──▶ Datadog Incidents API
```

## The scenario

Everything here is fictional. **Halyard** runs checkout and payments for online merchants. At
14:02 UTC `ledger-svc` v3.41.0 ships. It batches ledger writes and cuts the per-pod database pool
from 40 to 8, on the assumption that a pending change (INFRA-77) would scale the service from 8
to 32 replicas first. That change never merged. The ledger's total pool drops from 320
connections to 64, checkout's ledger calls queue and time out, and at 14:09 a p99 latency
monitor opens incident #4127.

Two other changes look just as guilty on a timeline: a tax-engine feature flag ramped from 10% to
50% at 13:55, and a web-frontend deploy went out at 13:40. The data rules them out. For example,
the warehouse shows checkouts failing at the same rate in both flag cohorts.

[`traces/example-run.jsonl`](traces/example-run.jsonl) is a recorded run triggered through the
webhook. It made 19 tool calls in 41 seconds, posted three timeline notes, set the incident's
root-cause field, and opened rollback request CR-5522 for `@dana.okafor` to approve.

## The pieces

| File | What it is |
| --- | --- |
| `agent.py` | The agent. Discovers the ops tools over MCP, adds three Datadog tools, and loops with the model until it calls `submit_findings`. Runs inside the sandbox. |
| `investigation.py` | Starts one sandbox per incident: an empty network allowlist plus three host-bound credentials. Then it runs the agent and streams its trace. |
| `responder.py` | Webhook receiver. Checks a shared secret, ignores Datadog's retries, caps concurrency, and serves each run's trace. |
| `run.py` | Runs the same investigation from your machine and writes `traces/<timestamp>.jsonl`. |
| `ops/server.py` | The internal ops MCP server: 11 tools over streamable HTTP, built with the official `mcp` SDK. It checks a bearer token and writes an audit line per call. |
| `ops/scenario.py` | The fixture data behind those tools: services, metrics, logs, traces, deploys, flags, runbooks and a 17k-row warehouse table. All of it is deterministic. |
| `incidents/server.py` | A stand-in for the Datadog Incidents and Monitors APIs. It uses the same paths and `DD-API-KEY` / `DD-APPLICATION-KEY` headers, and keeps what the agent writes. |
| `deploy_services.py`, `deploy_responder.py` | Deploy the three services as Chalk ScalingGroups. |

### What the ops MCP server exposes

`service_catalog`, `list_metrics`, `query_metrics`, `search_logs`, `trace_breakdown`,
`list_deploys`, `get_change`, `feature_flag_history`, `get_runbook` and `query_warehouse`
are read-only. `query_warehouse` takes one `SELECT` with external access disabled.

`request_rollback` is the only tool that writes, and it only opens a change request. The deploy
system acts on it after the owning team's on-call approves.

Swap the fixtures for your own systems and the agent does not change: it learns the tools from
the server at startup.

## What the sandbox can reach

```python
Sandbox(image=image, network_policy=NetworkPolicy(allowed_hosts=[]), ...)
    .add_credentials(chalk_api_host, Secret.from_local_env("CHALK_API_TOKEN"))
    .add_credentials(ops_mcp_host, Secret.from_local_env("OPS_MCP_TOKEN"))
    .add_credentials(datadog_host, Secret.from_local_env("DD_API_KEY"), Secret.from_local_env("DD_APP_KEY"))
```

Each credential's environment variable inside the sandbox holds a placeholder. The egress proxy
swaps in the real value, and only on requests to the host it is bound to. Before the agent
starts, a probe records what that means in practice:

```
$OPS_MCP_TOKEN in the sandbox                __chalk-secr... (placeholder)
POST https://api.github.com/gists            blocked (ConnectError)
POST https://hooks.slack.com/services/x      blocked (ConnectError)
POST https://<ops-mcp-host>/mcp (no token)   401
```

## Run it

You need [`uv`](https://docs.astral.sh/uv/) and a Chalk environment. Credentials come from
your existing `chalk login`.

```bash
./deploy_services.py # ops MCP server + Datadog API stand-in; writes .services.env
./run.py # investigate incidents/webhook.json from your machine
./deploy_responder.py # optional: answer real webhooks
```

To have Datadog start investigations, add a webhook in Datadog's Webhooks integration that
POSTs to `$RESPONDER_URL/webhooks/datadog`, with a custom header
`X-Webhook-Secret: $WEBHOOK_SECRET`. Notify it from an incident notification rule. The
responder expects a JSON payload shaped like
[`incidents/webhook.json`](incidents/webhook.json); `incident_id` (or `public_id`) is the only
required field. To use the real Datadog API instead of the stand-in, set `DD_API_URL` to your
site's API host (for example `https://api.datadoghq.com`) with real API and application keys.

To try the responder without Datadog:

```bash
set -a; . ./.services.env; set +a
curl -X POST "$RESPONDER_URL/webhooks/datadog" -H "X-Webhook-Secret: $WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' -d @incidents/webhook.json
curl -H "X-Webhook-Secret: $WEBHOOK_SECRET" "$RESPONDER_URL/runs/<run_id>"
```

## License

Apache-2.0.

## Local authentication

The SDK reads credentials, API host, and the selected environment from your existing
`chalk login` in `~/.chalk.yml`, including project-scoped and default logins.
For unattended runs, `CHALK_CLIENT_ID` and `CHALK_CLIENT_SECRET` remain supported.
The examples use the current environment selected by your Chalk login.

Requires `chalkcompute>=2.13.8` with the public connection context and in-memory
secret APIs from [chalk-sandbox-sdk#511](https://github.com/chalk-ai/chalk-sandbox-sdk/pull/511).
This PR is waiting for that SDK release.
