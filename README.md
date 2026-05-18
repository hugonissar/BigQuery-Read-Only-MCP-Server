# BigQuery Read-Only MCP Server

> A secure, self-hosted **Model Context Protocol (MCP) server for Google BigQuery**. Hard table allowlists, per-query scan ceilings, built-in rate limiting, and predictable costs on Cloud Run. Works with Claude, ChatGPT, Cursor, Gemini, and any MCP-compatible AI agent.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Cloud Run](https://img.shields.io/badge/runs%20on-Cloud%20Run-4285F4.svg)](https://cloud.google.com/run)
[![MCP](https://img.shields.io/badge/protocol-MCP-orange.svg)](https://modelcontextprotocol.io)
[![Claude Desktop](https://img.shields.io/badge/Claude_Desktop-Compatible-D97757?logo=claude&logoColor=fff)](https://claude.ai/download)

---

## 📚 Related writeups

Long-form posts on this project at [hugonissar.github.io](https://hugonissar.github.io):

- [**Self-hosted vs Google's official BigQuery MCP server: a security and cost comparison**](https://hugonissar.github.io/blog/bigquery-mcp-server-comparison/) — side-by-side feature comparison and when to pick each.
- [**Stopping the $2,000 AI query: how to cap BigQuery scan cost from an MCP server**](https://hugonissar.github.io/blog/bigquery-cost-control-mcp/) — the three layers of cost control with example IAM bindings.

Or read the [project overview](https://hugonissar.github.io/projects/bigquery-readonly-mcp-server/).

## 📖 What this is

A production-ready **BigQuery MCP server** you deploy to your own GCP project. AI agents connect over HTTPS and can do exactly two things: read the schemas of tables you explicitly allowlist, and run `SELECT` queries against them — subject to a configurable scan budget, a result-row cap, and a token-bucket rate limit. Nothing else. No `DROP`, no `INSERT`, no schema discovery beyond what you allow, no surprise billing.

It exists because the alternatives — including Google's official BigQuery MCP server — expose **every** table that the underlying service account can reach. That's the right default for trusted internal analysts. It's the wrong default for an autonomous agent that might be invoked by a customer-facing chatbot, a third-party tool, or a prompt-injected document.

## 💬 Use case

You: Give me the highest-converting campaign in the last 30 days<br />
LLM: *queries your data and returns the result*<br /><br />
Connect once, query everything. Point it at your Google Analytics 4 export, or import Google Ads, Meta, and TikTok into BigQuery to analyze every campaign across every channel from a single agent.

## 🏆 Why pick this over Google's official BigQuery MCP server

1. **Hard table allowlist enforced in code, not just IAM.** You list `(dataset, table)` pairs in env vars. Anything outside the allowlist is rejected at the SQL parser before a job is ever submitted — including qualified references like `wrong_dataset.allowed_table`. Google's server lets the agent see every table the SA's IAM permits.

2. **Hard per-query scan ceiling (`MAX_SCAN_MB`).** Every query is dry-run first. If the estimated scan exceeds the cap, the query is rejected — no BigQuery job is created, no bytes are billed. This stops the well-known "AI just ran a $2000 query" problem cold. Google's server [has no built-in scan ceiling](https://docs.cloud.google.com/bigquery/docs/use-bigquery-mcp) — you have to enforce it via custom IAM roles or BQ-level quotas.

3. **Built-in rate limiting with token-bucket + burst.** Google's docs are explicit: *"The BigQuery MCP server doesn't have its own quotas. There is no limit on the number of calls that can be made to the MCP server."* This server ships with configurable `RATE_LIMIT_QPM` and `RATE_LIMIT_BURST` out of the box, plus separate concurrency semaphores for queries vs. metadata calls.

4. **Read-only enforced by SQL parser, not just by IAM.** DDL, DML, scripting, multi-statement bodies, and procedural constructs are rejected at the application layer in addition to whatever IAM gives you. Defense in depth — a misconfigured IAM grant can't accidentally enable writes.

5. **Result-row cap (`MAX_RESULT_ROWS`).** A query returning a million rows gets truncated server-side before it ever touches an LLM context window. Saves tokens, saves money, prevents accidental PII exfiltration through wide scans.

6. **Dry-run cache.** Repeated dry-runs of the same SQL hit an LRU + TTL cache, so an agent that retries or iterates doesn't generate redundant BigQuery API calls. Schema lookups are similarly cached with TTL.

7. **Predictable, near-zero idle cost.** Cloud Run scales to zero. You pay roughly nothing at idle and a few dollars per month under modest load. Compare to a managed endpoint where you're tied to whatever pricing model the vendor lands on post-GA.

8. **Multi-tenant via env vars, no config file.** Comma-separated `BQ_DATASET_ID` and `BQ_ALLOWED_TABLE` pair positionally — `analytics.events`, `reporting.daily`, etc. Add or remove tables with a single `gcloud run deploy --update-env-vars`. No config file to bake into the image, no redeploys of a separate config service.

9. **Tiny tool surface.** Only two tools exposed: `get_table_schema` and `query_assessments`. Smaller attack surface, easier to audit, less for the agent to misuse. Google's server exposes a generic `execute_sql` plus metadata-discovery tools that walk the full project graph.

10. **One file, MIT licensed, ~1400 lines.** Read the source. Fork it. Add a custom validator. Swap the auth scheme. You can't do any of that with a managed closed-source server.

## ⚖️ Comparison table

| | This server | Google official BigQuery MCP
|---|---|---|
| **Deployment model** | Self-hosted on Cloud Run | Managed remote endpoint |
| **Auth** | API key + admin key | Google IAM / OAuth |
| **Table access control** | Hard allowlist of `(dataset, table)` pairs, enforced in code | IAM only — any reachable table |
| **Per-query scan cap** | Yes (`MAX_SCAN_MB`, enforced via dry-run) | No (relies on IAM / BQ quotas) |
| **Rate limiting** | Built-in token bucket + burst | None (per Google's docs) |
| **Result row cap** | Yes (`MAX_RESULT_ROWS`) | No |
| **Dry-run / schema cache** | Yes (LRU + TTL) | No |
| **Read-only enforcement** | SQL parser + IAM | IAM only |
| **Tools exposed** | 2 (schema, query) | 5+ including `execute_sql`, forecasting, dataset/table listing |
| **Forecasting / ML helpers** | No (write your own SQL) | Yes (built-in `forecast`) |
| **Prompt-injection scanning** | No | Yes (via Model Armor, paid add-on) |
| **Audit logging** | Cloud Logging + BQ audit logs | Cloud Audit Logs |
| **Source code** | Open (MIT) | Closed |
| **Idle cost** | ~$0 (scales to zero) | N/A (managed) |
| **Fork & modify** | Yes | No |
| **Region locked** | Choose any Cloud Run region | Google's regions only |

**When to pick Google's server instead:** if you want forecasting and ARIMA out of the box, if you need Model Armor for prompt-injection scanning, or if you're comfortable letting the agent see everything the SA's IAM reaches and you don't need a hard scan ceiling.

**When to pick this server:** anything else — especially production agents, customer-facing deployments, regulated environments, multi-tenant scenarios, and anywhere the words "scan budget" or "rate limit" matter.

## 🏗️ Architecture at a glance

```
┌─────────────┐       HTTPS + X-API-KEY       ┌──────────────────────┐         IAM        ┌───────────┐
│ MCP client  │ ────────────────────────────▶ │  Cloud Run service   │ ─────────────────▶ │ BigQuery  │
│ (Claude /   │            API KEY            │  bigquery-readonly-  │   service account  │  datasets │
│  Cursor /   │                               │       mcp-server     │                    │  + tables │
│  ChatGPT)   │                               │                      │                    │           │
└─────────────┘                               │  • SQL allowlist     │                    └───────────┘
                                              │  • Dry-run scan cap  │
                                              │  • Rate limiter      │                    ┌───────────┐
                                              │  • Schema cache      │ ◀───── secrets ──  │  Secret   │
                                              └──────────────────────┘                    │  Manager  │
                                                                                          └───────────┘
```

## ☁️ Required GCP services

Enable these APIs in your project:

| Service | Purpose | Required |
|---|---|---|
| **Cloud Run** (`run.googleapis.com`) | Hosts the MCP server | Yes |
| **BigQuery** (`bigquery.googleapis.com`) | The data warehouse you're querying | Yes |
| **Secret Manager** (`secretmanager.googleapis.com`) | Stores `MCP_API_KEY` and `MCP_ADMIN_KEY` | Yes |
| **Artifact Registry** (`artifactregistry.googleapis.com`) | Hosts the container image | Yes |
| **IAM** (`iam.googleapis.com`) | Service account + role bindings | Yes |
| **Cloud Build** (`cloudbuild.googleapis.com`) | If building the image in GCP | Optional |
| **Cloud Logging** | Captures structured logs from the service | Auto |
| **Cloud Monitoring** | Captures metrics + alerts | Auto |

Quick enable:

```bash
gcloud services enable \
  run.googleapis.com \
  bigquery.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  cloudbuild.googleapis.com
```

## 🔐 Service account permissions (least privilege)

Create a dedicated service account — **do not** reuse one. The service needs the absolute minimum: read access to specific BigQuery tables, the ability to run query jobs, and read access to two secrets.

```bash
PROJECT_ID="your-project"
SA_NAME="bigquery-readonly-mcp"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud iam service-accounts create $SA_NAME \
  --display-name="BigQuery Read-Only MCP Server"
```

### 🔑 Required IAM roles

| Role | Scope | Why |
|---|---|---|
| `roles/bigquery.jobUser` | **Project** | Lets the SA submit query jobs (does not grant data access) |
| `roles/bigquery.dataViewer` | **Dataset** (per allowlisted dataset) | Read schemas + table rows |
| `roles/bigquery.metadataViewer` | **Dataset** (per allowlisted dataset) | Read schemas without read access — optional, only if you have datasets where you want schema visibility without row access |
| `roles/secretmanager.secretAccessor` | **Secret** (per secret) | Read API key + admin key |

#### Grant `bigquery.jobUser` on the project

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.jobUser"
```

#### Grant `bigquery.dataViewer` on the specific dataset (preferred over project-wide)

```bash
# For each dataset in BQ_DATASET_ID:
DATASET="your_dataset"
bq add-iam-policy-binding \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataViewer" \
  "${PROJECT_ID}:${DATASET}"
```

For tighter control, grant access at the **table** level instead of the dataset:

```bash
DATASET="your_dataset"
TABLE="your_table"
bq add-iam-policy-binding \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.dataViewer" \
  "${PROJECT_ID}:${DATASET}.${TABLE}"
```

#### Grant `secretAccessor` on the two secrets

```bash
gcloud secrets add-iam-policy-binding mcp-api-key \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding mcp-admin-key \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor"
```

That's the entire IAM footprint. Do **not** grant `bigquery.user`, `bigquery.admin`, `editor`, or `owner` — none of those are needed and all of them grant strictly more than necessary.

## 🚀 Quick start

### 1. Generate and store API keys

```bash
# MCP API key (clients use this in X-API-KEY)
openssl rand -hex 32 | gcloud secrets create mcp-api-key --data-file=-

# Admin key (for the /admin endpoint — schema cache invalidation)
openssl rand -hex 32 | gcloud secrets create mcp-admin-key --data-file=-
```

### 2. Build and push the image

```bash
REGION="europe-north2"
PROJECT_ID="your-project"
REPO="bigquery-readonly-mcp"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/server:latest"

gcloud artifacts repositories create $REPO \
  --repository-format=docker \
  --location=$REGION

gcloud builds submit --tag $IMAGE
```

### 3. Deploy to Cloud Run

```bash
gcloud run deploy bigquery-readonly-mcp \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="bigquery-readonly-mcp@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-secrets="MCP_API_KEY=mcp-api-key:latest,MCP_ADMIN_KEY=mcp-admin-key:latest" \
  --set-env-vars="\
GCP_PROJECT_ID=${PROJECT_ID},\
BQ_DATASET_ID=your_dataset,\
BQ_ALLOWED_TABLE=your_table,\
MAX_SCAN_MB=100,\
MAX_RESULT_ROWS=2000,\
BQ_JOB_TIMEOUT_SECS=60,\
MAX_SQL_LENGTH=2000,\
SCHEMA_TTL_SECS=300,\
DRY_RUN_CACHE_TTL_SECS=60,\
DRY_RUN_CACHE_MAX_ENTRIES=1000,\
RATE_LIMIT_QPM=20,\
RATE_LIMIT_BURST=5,\
QUERY_CONCURRENCY=10,\
META_CONCURRENCY=3,\
ADMIN_RATE_LIMIT_QPM=10,\
MAX_REQUEST_BODY_BYTES=65536" \
  --allow-unauthenticated \
  --port 8080
```

For **multiple datasets/tables**, gcloud needs an alternate delimiter so commas inside values aren't split as separate vars:

```bash
gcloud run deploy bigquery-readonly-mcp \
  --image="$IMAGE" \
  ...
  --set-env-vars="^@^\
GCP_PROJECT_ID=${PROJECT_ID}@\
BQ_DATASET_ID=analytics,reporting,raw@\
BQ_ALLOWED_TABLE=events,daily_summary,events@\
MAX_SCAN_MB=100"
```

Datasets and tables are paired **positionally**: index 0 pairs with index 0. The example above allows `analytics.events`, `reporting.daily_summary`, and `raw.events`. List lengths must match.

### 4. Connect from an MCP client

```jsonc
// claude_desktop_config.json or equivalent
{
  "mcpServers": {
    "bigquery": {
      "command": "/home/yourusername/.local/bin/uvx",
      "args": [
        "mcp-proxy",
        "--transport", "streamablehttp",
        "-H", "x-api-key", "YOUR_MCP_API_KEY",
        "https://bigquery-readonly-mcp-XXXX.a.run.app/mcp"
      ]
    }
  }
}
```

## ⚙️ Configuration reference

All configuration is via environment variables. Required vars abort startup if missing; everything else has a default.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GCP_PROJECT_ID` | Yes | — | The project where BigQuery jobs run |
| `BQ_DATASET_ID` | Yes | — | Comma-separated list of datasets |
| `BQ_ALLOWED_TABLE` | Yes | — | Comma-separated list of tables; paired positionally with `BQ_DATASET_ID` |
| `MCP_API_KEY` | Yes | — | API key clients must present |
| `MCP_ADMIN_KEY` | No | — | API key for `/admin` endpoint; if empty, defaults to MCP_API_KEY |
| `MAX_SCAN_MB` | No | `100` | Hard ceiling on bytes scanned per query (MB) |
| `MAX_RESULT_ROWS` | No | `2000` | Result rows are truncated above this |
| `BQ_JOB_TIMEOUT_SECS` | No | `30` | Per-query BigQuery timeout |
| `MAX_SQL_LENGTH` | No | `2000` | Reject SQL longer than this |
| `MAX_REQUEST_BODY_BYTES` | No | `65536` | Reject HTTP bodies larger than this |
| `SCHEMA_TTL_SECS` | No | `300` | Schema cache TTL |
| `DRY_RUN_CACHE_TTL_SECS` | No | `60` | Dry-run cache TTL |
| `DRY_RUN_CACHE_MAX_ENTRIES` | No | `1000` | LRU size for dry-run cache |
| `RATE_LIMIT_QPM` | No | `20` | Token-bucket refill rate (queries per minute) |
| `RATE_LIMIT_BURST` | No | `5` | Token-bucket burst capacity |
| `QUERY_CONCURRENCY` | No | `10` | Max concurrent BQ query jobs |
| `META_CONCURRENCY` | No | `3` | Max concurrent metadata calls |
| `ADMIN_RATE_LIMIT_QPM` | No | `10` | Separate rate limit for admin endpoint |

## 🛡️ Security model

The threat model is: **an AI agent is partially or fully untrusted, and may be invoked with adversarial input.** The controls are layered.

1. **Network layer.** Cloud Run gives you HTTPS termination, optional Cloud Armor in front for IP allowlisting or WAF rules.
2. **Auth layer.** Every request requires an `x-api-key: <key>` header matched against `MCP_API_KEY` in constant time. Admin endpoint requires a separate `x-admin-key` header.
3. **Body size limit.** Requests above `MAX_REQUEST_BODY_BYTES` are rejected before parsing.
4. **Rate limit.** Token-bucket, per-instance. Misbehaving clients get `429`s.
5. **SQL parser layer.** `sqlparse` decomposes every query. Non-`SELECT` statements, multi-statement bodies, DDL, DML, scripting, and procedural constructs are rejected.
6. **Allowlist enforcement.** Every table referenced in `FROM` / `JOIN` / comma-join is validated against the `(dataset, table)` allowlist on its last two segments. Cross-dataset references like `wrong_ds.allowed_table` are rejected. Bare table names that exist in multiple allowed datasets are rejected as ambiguous (the agent must qualify).
7. **CTE awareness.** Common Table Expression names are exempt — they're not tables.
8. **Dry-run scan ceiling.** BigQuery's dry-run estimates bytes scanned. Queries exceeding `MAX_SCAN_MB` are rejected before the real job runs.
9. **IAM layer.** Even if every above check were bypassed, the service account only has `dataViewer` on specific datasets / tables.
10. **Result truncation.** Output is capped at `MAX_RESULT_ROWS` so a single response can't exfiltrate an entire table.

What the server does **not** do (and you should know):

- **No prompt-injection scanning.** If you need this, deploy behind a model security gateway (e.g. Google's Model Armor, Lakera Guard, NeMo Guardrails) or use Google's official MCP server which has Model Armor integration.
- **No column-level masking.** The allowlist is at table granularity. If your tables contain PII columns you don't want exposed, create a BigQuery authorized view that projects only safe columns and allowlist the view.
- **No per-user attribution.** The API key is shared across clients. For per-user audit trails, put an identity-aware proxy in front, or fork and add OAuth.
- **No write support, ever.** This is intentional. If you need writes, use a different server.

## 💻 Local development

```bash
git clone https://github.com/hugonissar/bigquery-readonly-mcp-server.git
cd bigquery-readonly-mcp-server

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Authenticate to GCP
gcloud auth application-default login

# Set required env vars
export GCP_PROJECT_ID=your-project
export BQ_DATASET_ID=your_dataset
export BQ_ALLOWED_TABLE=your_table
export MCP_API_KEY=$(openssl rand -hex 32)

# Run
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

The MCP endpoint will be at `http://localhost:8080/mcp` and `/health` returns service status.

## 🛠️ Operations

### 🔄 Invalidate the schema cache after a schema change

```bash
curl -X POST "https://your-service.run.app/admin/invalidate-cache" \
  -H "x-admin-key: $MCP_ADMIN_KEY"
```

### 📜 Tail logs

```bash
gcloud run services logs tail bigquery-readonly-mcp --region=$REGION
```

### 🔍 Query the audit trail

Every BigQuery job is recorded in Cloud Audit Logs. To see what queries the MCP server has run:

```sql
SELECT
  protopayload_auditlog.authenticationInfo.principalEmail AS sa,
  protopayload_auditlog.servicedata_v1_bigquery.jobCompletedEvent.job.jobConfiguration.query.query AS sql,
  protopayload_auditlog.servicedata_v1_bigquery.jobCompletedEvent.job.jobStatistics.totalBilledBytes AS bytes,
  timestamp
FROM `your-project.cloudaudit_logs.cloudaudit_googleapis_com_data_access`
WHERE protopayload_auditlog.authenticationInfo.principalEmail = 'bigquery-readonly-mcp@your-project.iam.gserviceaccount.com'
ORDER BY timestamp DESC
LIMIT 100;
```

## ⚠️ Limitations

- **One region per deployment.** Cloud Run is regional. For multi-region failover, deploy multiple instances behind a global load balancer.
- **Cold starts.** Scale-to-zero means the first request after idle takes a few seconds. Set `--min-instances=1` to eliminate this at the cost of a few dollars per month.
- **Single auth scheme.** API key in `x-api-key` header only. No OAuth, no mTLS out of the box (both are achievable via Cloud Run's IAM-based auth + an identity-aware proxy — see Operations).
- **Schema cache is per-instance.** Each Cloud Run instance maintains its own. The admin endpoint invalidates the cache on the instance that receives the call; under load with multiple instances, you may want to roll a new revision instead.
- **No streaming results.** Queries materialize fully server-side before truncation. Don't increase `MAX_RESULT_ROWS` beyond a few thousand without considering memory.

## ❓Frequently asked questions

**Does this work with Claude Desktop?** Yes, via the `url` + `headers` config above. Also works with Cursor, Windsurf, Claude Code, the OpenAI Responses API, and anything else that speaks streamable-HTTP MCP.

**Can I use this with a private VPC / serverless VPC connector?** Yes. Add `--vpc-connector` and `--ingress=internal` to the Cloud Run deploy command. Then attach an internal load balancer for client access.

**How does this compare to MCP Toolbox for Databases?** MCP Toolbox is generic across many databases and configured via YAML. This server is BigQuery-specific and configured via env vars. Pick Toolbox if you need multi-database; pick this if you want BigQuery-specific guardrails (scan caps, allowlists) baked in.

**Can I add custom tools?** Yes — it's one Python file. Add a new `@mcp.tool` and apply the same validation pattern.

**Is the schema response format stable?** `get_table_schema` returns `{"tables": [{dataset, table, partition_field, clustering_fields, schema}, ...]}`. The shape is stable across single-table and multi-table configurations.

**Does it support `bq` legacy SQL?** No. Standard SQL (GoogleSQL) only.

## 🤝 Contributing

PRs welcome. Issues with reproductions get prioritized.

## 📄 License

MIT. See [LICENSE](./LICENSE).

---

<sub>Keywords: BigQuery MCP server, Model Context Protocol BigQuery, Claude BigQuery integration, secure BigQuery MCP, self-hosted BigQuery MCP, Cloud Run MCP server, BigQuery AI agent, read-only BigQuery, BigQuery LLM, MCP server Cloud Run, BigQuery rate limiting, BigQuery cost control AI.</sub>
