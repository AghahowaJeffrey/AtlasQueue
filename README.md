# AtlasQueue

> **A production-grade distributed task queue** built on PostgreSQL + Redis — demonstrating durable job delivery, atomic lease acquisition, exponential retry with jitter, and dead-letter handling. Designed to show the systems-design and reliability thinking expected at senior/staff level.

[![Build](https://img.shields.io/badge/build-passing-brightgreen)](#running-tests)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Guarantees](#guarantees)
3. [Design Decisions & Tradeoffs](#design-decisions--tradeoffs)
4. [Failure Modes & Mitigations](#failure-modes--mitigations)
5. [Job State Machine](#job-state-machine)
6. [Retry Policy](#retry-policy)
7. [Observability](#observability)
8. [Quick Start](#quick-start)
9. [API Reference](#api-reference)
10. [Project Structure](#project-structure)
11. [Configuration](#configuration)
12. [Running Tests](#running-tests)
13. [Load Test Results](#load-test-results)

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                           CLIENT                                    │
│  POST /v1/jobs   Idempotency-Key: <caller-uuid>                     │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ HTTPS
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      API SERVICE  (FastAPI / uvicorn)               │
│                                                                     │
│  1. BEGIN TRANSACTION                                               │
│  2. SELECT idempotency_keys WHERE key = ?  ──► duplicate? return    │
│  3. INSERT INTO jobs (status='queued')                              │
│  4. INSERT INTO idempotency_keys                                    │
│  5. COMMIT                                                          │
│  6. LPUSH job_id → Redis  (post-commit, at-least-once)             │
│                                                                     │
│  GET /v1/jobs/:id  ──► SELECT jobs WHERE id = ?                    │
│  GET /metrics      ──► Prometheus text format                       │
└──────────────┬────────────────────────────┬────────────────────────┘
               │ SQL (asyncpg)              │
               ▼                            ▼
┌──────────────────────┐       ┌──────────────────────────┐
│   PostgreSQL 16      │       │        Redis 7           │
│                      │       │                          │
│  jobs                │       │  atlasqueue:jobs  LIST   │
│  idempotency_keys    │◄──────│  atlasqueue:dlq   LIST   │
│                      │ lease │                          │
└──────────────────────┘ guard └──────────┬───────────────┘
               ▲                          │ BLPOP
               │                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     WORKER SERVICE  (asyncio)                        │
│                                                                      │
│  BLPOP atlasqueue:jobs  ──► job_id                                   │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  LEASE ACQUISITION  (atomic UPDATE…RETURNING)               │    │
│  │                                                             │    │
│  │  UPDATE jobs SET                                            │    │
│  │    status='running',                                        │    │
│  │    locked_until = now() + lease_seconds,                    │    │
│  │    lock_owner  = worker_id,                                 │    │
│  │    attempts    = attempts + 1                               │    │
│  │  WHERE id = ?                                               │    │
│  │    AND status IN ('queued','failed')                        │    │
│  │    AND run_at <= now()                                      │    │
│  │    AND (locked_until IS NULL OR locked_until < now())       │    │
│  │  RETURNING *                                                │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  handler(payload)         ── registered via @register("job.type")   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ ON SUCCESS  → UPDATE status='succeeded'                      │   │
│  │ ON FAILURE  → UPDATE status='failed', run_at=backoff         │   │
│  │ ON EXHAUSTED→ UPDATE status='dead', LPUSH dlq                │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  BLPOP timeout → sweep_failed_jobs()                                 │
│    SELECT id WHERE status='failed' AND run_at <= now()               │
│    FOR UPDATE SKIP LOCKED   ──► LPUSH → UPDATE status='queued'      │
│                                                                      │
│  Prometheus sidecar HTTP server on :9091                            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Guarantees

### At-Least-Once Delivery

```
Job is written to Postgres BEFORE Redis is touched.
If Redis LPUSH fails → job stays status=queued.
Worker sweep re-enqueues all queued/failed jobs with run_at ≤ now.
Result: a job is never silently dropped.
```

Duplicate delivery is possible (e.g., worker crashes after claiming but before marking `succeeded`). The lease guard absorbs this:
- Expired `locked_until < now` → next worker can reclaim.
- Succeeded/dead jobs don't match the `WHERE status IN ('queued','failed')` predicate → safe no-op.

### Idempotent Submission

Every `POST /v1/jobs` requires an `Idempotency-Key` header. The flow:

```
SELECT idempotency_keys WHERE key = <caller-key>
   found  → return existing job (200 OK)
   not found → INSERT job + key atomically (201 Created) → LPUSH
```

The `idempotency_keys` primary key constraint acts as a last-resort guard against concurrent duplicate inserts — only one INSERT wins; the loser gets an `IntegrityError`.

### Exactly-Once Execution (best-effort)

Not guaranteed (distributed systems can't without 2PC), but the system is designed to make duplicate execution detectable:
- `job.attempts` is the authoritative attempt counter, incremented atomically in the same `UPDATE` that claims the lease.
- Handlers receive the full `job` object including `attempts`, so they can implement their own idempotency if needed.

---

## Design Decisions & Tradeoffs

### Why Redis `LIST` (not a `ZSET`) for the main queue?

| | `LIST` (LPUSH/BLPOP) | `ZSET` (score=run_at) |
|---|---|---|
| **Blocking pop** | ✅ Native `BLPOP` — no polling | ❌ Must poll with `ZRANGEBYSCORE` |
| **Ordered retry scheduling** | ❌ Not native | ✅ Score = `run_at` timestamp |
| **Simplicity** | ✅ Two commands | More complex |
| **Our solution** | Main queue uses LIST | Retries use Postgres `run_at` + DB sweep |

**Decision**: Main queue is a Redis `LIST` — BLPOP eliminates polling busy-loops. Retry scheduling is handled by storing `run_at` in Postgres and using a DB sweep (`SELECT FOR UPDATE SKIP LOCKED`) rather than a Redis ZSET. This keeps Redis as a simple signalling layer and Postgres as the durable source of truth.

### Why Postgres for leasing instead of Redis `SET NX`?

| | Postgres `UPDATE…WHERE…RETURNING` | Redis `SET NX` + TTL |
|---|---|---|
| **Atomicity** | ✅ Single statement — no TOCTOU | ⚠️ Two operations (SET + expire) |
| **Crash recovery** | ✅ `locked_until < now` predicate built-in | ⚠️ TTL can expire before app notices |
| **Audit trail** | ✅ `lock_owner`, `attempts`, `last_error` in same row | ❌ Separate store |
| **Multi-worker safety** | ✅ Postgres row-level locking | ✅ Redis is single-threaded |

**Decision**: Postgres lease via `UPDATE…RETURNING` — eliminates TOCTOU races entirely. The row is the lock. `locked_until` gives automatic crash recovery without a separate TTL mechanism.

### Why asyncio throughout (not Celery or similar)?

- **No broker abstraction needed** — the system _is_ the broker; full control of delivery semantics.
- **No serialization overhead** — jobs are native SQLAlchemy objects.
- **Single-process async** — one worker process handles hundreds of concurrent lease checks via the event loop, no threading complexity.
- **Tradeoff**: no built-in task routing, rate-limiting, or canvas — these would need to be built for v2.

### Why structlog + Prometheus over an APM agent?

- APM agents (Datadog, New Relic) add vendor lock-in and ~2–5ms per-request latency.
- `structlog` JSON logs are sink-agnostic — ship to any log aggregator (Loki, Splunk, CloudWatch).
- `prometheus_client` is language-native — no agent, no network call on the hot path.

---

## Failure Modes & Mitigations

| Scenario | What Happens | Recovery |
|----------|-------------|----------|
| **API crashes after DB COMMIT, before Redis LPUSH** | Job is `queued` in Postgres with no Redis entry | Worker sweep re-enqueues all `queued` jobs with `run_at ≤ now` on next BLPOP timeout |
| **Worker crashes mid-handler** | Job stays `running` with `locked_until = T` | After `T` expires, any worker's lease attempt matches `locked_until < now` and reclaims |
| **Redis unavailable at submit time** | `LPUSH` raises — returns 500 to caller | Caller retries with same `Idempotency-Key`. Worker sweep covers any that slipped through |
| **Redis unavailable during sweep LPUSH** | `sweep_failed_jobs` propagates exception | Caught in consumer loop, logged, retried on next timeout |
| **Postgres unavailable** | Both API and worker fail fast with a logged error | No silent data loss — Postgres is the source of truth |
| **Handler panics (unhandled exception)** | `execute_job` catches it, transitions `running → failed`, stores `last_error` | Retried up to `max_attempts`, then moved to DLQ |
| **Duplicate Redis message** (at-least-once) | Second worker's `UPDATE` matches 0 rows (job already `running`/`succeeded`) | `LEASE_REJECTED` counter incremented; job skipped silently |
| **Multiple workers sweep simultaneously** | `SELECT FOR UPDATE SKIP LOCKED` ensures non-overlapping row sets | Each worker gets a disjoint set of due-retry jobs |
| **Enormous handler error message** | Truncated to 4096 chars before storage | Prevents `last_error` column overflow |

---

## Job State Machine

```
                 ┌─────────────────────────┐
                 │        queued           │◄──── LPUSH (submit / sweep)
                 └────────────┬────────────┘
                              │  acquire_lease()
                              │  UPDATE…RETURNING (atomic)
                              ▼
                 ┌─────────────────────────┐
                 │        running          │
                 └──┬─────────────────┬───┘
                    │ success         │ failure
                    ▼                 ▼
       ┌─────────────────┐  ┌─────────────────────────┐
       │    succeeded    │  │   failed                │
       │  (terminal)     │  │   run_at = now+backoff  │
       └─────────────────┘  └──────────┬──────────────┘
                                       │ attempts < max_attempts
                                       │ sweep re-enqueues
                                       ▼
                            ┌─────────────────────────┐
                            │       queued            │ (retry cycle)
                            └─────────────────────────┘
                                       │ attempts >= max_attempts
                                       ▼
                            ┌─────────────────────────┐
                            │         dead            │
                            │  LPUSH atlasqueue:dlq   │
                            │  (terminal)             │
                            └─────────────────────────┘
```

---

## Retry Policy

Exponential backoff with full jitter (PRD §9):

```
run_at = now + (base_delay × 2^attempts) + random(0, jitter_max)
```

| Variable | Default | Env var |
|----------|---------|---------|
| `base_delay` | 5 s | `RETRY_BASE_DELAY_SECONDS` |
| `jitter_max` | 2 s | `RETRY_JITTER_MAX_SECONDS` |
| `max_attempts` | per-job | set at submission time |

**Example** (base=5, jitter=0 for clarity):

| Attempt | Delay |
|---------|-------|
| 1 | 5 × 2¹ = **10 s** |
| 2 | 5 × 2² = **20 s** |
| 3 | 5 × 2³ = **40 s** |
| 4 | 5 × 2⁴ = **80 s** |

Full jitter (not truncated jitter) is used to prevent [thundering herd](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) when many jobs fail simultaneously.

---

## Observability

### Structured Logs (structlog — JSON)

Every key event emits a structured log line with consistent fields:

```json
{"event": "job_created",     "job_id": "...", "job_type": "email.send", "level": "info"}
{"event": "lease_acquired",  "job_id": "...", "attempt": 1, "locked_until": "...", "worker_id": "..."}
{"event": "handler_start",   "job_id": "...", "job_type": "email.send", "attempt": 1}
{"event": "job_succeeded",   "job_id": "...", "level": "info"}
{"event": "job_failed",      "job_id": "...", "retry_at": "...", "error": "...", "level": "warning"}
{"event": "job_dead",        "job_id": "...", "dlq_key": "atlasqueue:dlq", "level": "error"}
{"event": "sweep_complete",  "requeued": 3,   "level": "info"}
```

### Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `atlasqueue_jobs_submitted_total` | Counter | `job_type` | New jobs submitted |
| `atlasqueue_jobs_succeeded_total` | Counter | `job_type` | Successful completions |
| `atlasqueue_jobs_failed_total` | Counter | `job_type` | Retryable failures |
| `atlasqueue_jobs_dead_total` | Counter | `job_type` | Jobs exhausted retries → DLQ |
| `atlasqueue_jobs_idempotent_total` | Counter | `job_type` | Duplicate submissions absorbed |
| `atlasqueue_jobs_retried_total` | Counter | — | Jobs re-enqueued by sweep |
| `atlasqueue_lease_acquired_total` | Counter | — | Successful lease acquisitions |
| `atlasqueue_lease_rejected_total` | Counter | — | Contested/non-claimable leases |
| `atlasqueue_worker_active_jobs` | Gauge | — | Jobs currently executing |
| `atlasqueue_job_duration_seconds` | Histogram | `job_type`, `outcome` | Handler wall-clock time |
| `atlasqueue_http_request_duration_seconds` | Histogram | `method`, `path_template`, `status_code` | API latency |
| `atlasqueue_http_requests_total` | Counter | `method`, `path_template`, `status_code` | Total HTTP requests |

**Endpoints:**
- API: `GET http://localhost:8000/metrics/`
- Worker: `GET http://localhost:9091/`

---

## Quick Start

### Prerequisites

- Docker ≥ 24 + Docker Compose v2
- `curl` and `python3` for smoke tests

### 1 — Boot everything

```bash
git clone https://github.com/AghahowaJeffrey/AtlasQueue.git
cd AtlasQueue

# Start Postgres, Redis, run migrations, then API + worker
docker compose up --build
```

### 2 — Verify health

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
# → {"status": "ok", "service": "atlasqueue-api"}
```

### 3 — Submit a job

```bash
# New job — 201 Created
curl -s -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-001" \
  -d '{"type": "noop", "payload": {"hello": "world"}, "max_attempts": 3}' \
  | python3 -m json.tool
```

Response:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "noop",
  "status": "queued",
  "attempts": 0,
  "max_attempts": 3,
  "payload": {"hello": "world"},
  "run_at": "2026-03-03T00:00:00Z",
  "created_at": "2026-03-03T00:00:00Z"
}
```

### 4 — Poll status

```bash
curl -s http://localhost:8000/v1/jobs/<JOB_ID> | python3 -m json.tool
# status: "queued" → "running" → "succeeded" (within ~1s)
```

### 5 — Resubmit with same key (idempotency)

```bash
# Same Idempotency-Key → 200 OK, same job returned — no duplicate
curl -s -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-001" \
  -d '{"type": "noop", "payload": {"hello": "world"}, "max_attempts": 3}'
```

### 6 — Watch worker logs

```bash
docker compose logs -f worker
```

### 7 — Inspect DLQ

```bash
# Submit a job that will always fail (unregistered type)
curl -X POST http://localhost:8000/v1/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: dlq-test-001" \
  -d '{"type": "no.handler", "payload": {}, "max_attempts": 2}'

# After retries exhaust, check DLQ
docker compose exec redis redis-cli LRANGE atlasqueue:dlq 0 -1
```

### 8 — Tear down

```bash
docker compose down        # keep data
docker compose down -v     # wipe volumes
```

---

## API Reference

### `POST /v1/jobs`

Submit a new job. Idempotent on `Idempotency-Key`.

**Headers**

| Header | Required | Description |
|--------|----------|-------------|
| `Idempotency-Key` | ✅ | Caller-generated unique key (1–512 chars) |
| `Content-Type` | ✅ | `application/json` |

**Body**

```json
{
  "type": "email.send",
  "payload": { "to": "user@example.com" },
  "max_attempts": 5
}
```

| Field | Type | Constraints | Default |
|-------|------|-------------|---------|
| `type` | string | 1–128 chars | required |
| `payload` | object | any JSON object | required |
| `max_attempts` | integer | 1–25 | `5` |

**Responses**

| Code | Meaning |
|------|---------|
| `201 Created` | New job created and enqueued |
| `200 OK` | Duplicate — existing job returned |
| `422 Unprocessable Entity` | Validation error (missing header, bad body) |

---

### `GET /v1/jobs/{job_id}`

Retrieve a job by UUID.

**Responses**

| Code | Meaning |
|------|---------|
| `200 OK` | Job found |
| `404 Not Found` | No job with that ID |
| `422 Unprocessable Entity` | Malformed UUID |

---

### `GET /health`

```json
{"status": "ok", "service": "atlasqueue-api"}
```

---

## Registering Handlers

Add a handler in `atlasqueue/worker/handlers.py` or any module imported at worker startup:

```python
from atlasqueue.worker.handlers import register

@register("email.send")
async def send_email(payload: dict) -> None:
    """Raise any exception to signal failure and trigger retry."""
    recipient = payload["to"]
    # ... call mail API

@register("invoice.generate")
async def generate_invoice(payload: dict) -> None:
    invoice_id = payload["invoice_id"]
    # ...
```

Then rebuild the worker:
```bash
docker compose up --build worker
```

---

## Project Structure

```
atlasqueue/
├── atlasqueue/
│   ├── core/
│   │   ├── config.py        # pydantic-settings — all env vars
│   │   ├── enums.py         # JobStatus enum (queued→running→succeeded/failed/dead)
│   │   ├── logging.py       # structlog JSON config
│   │   └── metrics.py       # Prometheus metric definitions (single source of truth)
│   ├── db/
│   │   ├── models.py        # SQLAlchemy ORM (Job, IdempotencyKey)
│   │   └── session.py       # async engine + session factory
│   ├── api/
│   │   ├── main.py          # FastAPI app factory, HTTP metrics middleware, /metrics mount
│   │   ├── deps.py          # DI: get_db (plain session), get_redis
│   │   ├── schemas.py       # Pydantic request/response models
│   │   └── routers/
│   │       ├── health.py    # GET /health
│   │       └── jobs.py      # POST /v1/jobs, GET /v1/jobs/{id}
│   ├── services/
│   │   └── jobs.py          # submit_job (idempotency + enqueue), get_job
│   └── worker/
│       ├── main.py          # Entry point, Prometheus sidecar on :9091
│       ├── consumer.py      # BLPOP loop + sweep on timeout
│       ├── lease.py         # acquire_lease() — atomic UPDATE…RETURNING
│       ├── executor.py      # execute_job() — dispatch + status transitions
│       ├── retry.py         # compute_backoff() + sweep_failed_jobs()
│       └── handlers.py      # @register decorator + built-in noop handler
├── migrations/
│   └── versions/
│       └── 0001_initial.py  # jobs + idempotency_keys tables
├── tests/
│   ├── conftest.py          # shared fixtures (mock_session, mock_redis, client)
│   ├── integration/
│   │   └── test_job_submission.py   # 11 tests — API layer
│   └── unit/
│       ├── test_lease.py            # 7 tests — acquire_lease + consumer dispatch
│       ├── test_executor.py         # 5 tests — status transition paths
│       ├── test_retry.py            # 7 tests — backoff formula + sweep
│       └── test_metrics.py          # 7 tests — metric registration + /metrics endpoint
├── Dockerfile.api
├── Dockerfile.worker
├── docker-compose.yml
├── pyproject.toml
└── .env.docker
```

---

## Configuration

All settings via environment variables (see `.env.docker`):

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async Postgres DSN |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `JOB_QUEUE_KEY` | `atlasqueue:jobs` | Main queue Redis key |
| `DLQ_KEY` | `atlasqueue:dlq` | Dead-letter queue Redis key |
| `WORKER_LEASE_SECONDS` | `30` | Lease TTL — auto-reclaim after crash |
| `WORKER_POLL_TIMEOUT` | `5` | BLPOP timeout → triggers sweep |
| `WORKER_METRICS_PORT` | `9091` | Worker Prometheus sidecar port |
| `RETRY_BASE_DELAY_SECONDS` | `5` | Backoff base delay |
| `RETRY_JITTER_MAX_SECONDS` | `2` | Max random jitter added to backoff |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |

---

## Running Tests

Tests are fully mocked — no Postgres or Redis required.

```bash
# Install dev dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements/dev.txt

# Run all 37 tests
pytest tests/ -v

# By layer
pytest tests/unit/        -v   # 26 unit tests
pytest tests/integration/ -v   # 11 integration tests (API layer)
```

**Current results:**
```
37 passed in 1.89s
```

---

## Load Test Results

> 🚧 Load tests are planned for a future milestone using [k6](https://k6.io). Results will be published here including:
> - Sustained throughput (jobs/sec at p99 < 200ms)
> - Spike behaviour (5× traffic burst)
> - Worker scale-out (horizontal, multiple consumer processes)
> - Redis and Postgres saturation points

---

## Tech Stack

| Layer | Technology | Why |
|-------|-----------|-----|
| **API** | FastAPI + uvicorn | Async-native, auto OpenAPI, fast |
| **ORM** | SQLAlchemy 2 (asyncpg) | Async-first, type-safe, mature |
| **Queue signalling** | Redis 7 `LIST` | Native BLPOP, no polling |
| **Persistence** | PostgreSQL 16 | ACID, row-level locks, `SKIP LOCKED` |
| **Migrations** | Alembic | Battle-tested, async env support |
| **Logging** | structlog | JSON-first, sink-agnostic |
| **Metrics** | prometheus-client | Zero-overhead, Grafana-compatible |
| **Config** | pydantic-settings | Type-safe env var parsing |
| **Runtime** | Python 3.12 | async/await, performance improvements |

---

## License

MIT
