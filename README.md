# AtlasQueue

> A reliability-focused distributed task processing engine demonstrating production-grade async job execution patterns.

```
Client
  │
  ▼
FastAPI Service ──────────────────────────┐
  │   (atomic: DB insert + idempotency)   │
  ▼                                       │
PostgreSQL ◄─────────────────────────────┤
  │                                       │
  ▼                                       │
Redis Queue ◄───────────────────────────┐│
  │                                     ││
  ▼                                     ││
Worker Service                          ││
  │  ● Lease acquisition (locked_until) ││
  │  ● Handler execution                ││
  │  ● Exponential retry + jitter       ││
  │  ● Dead-letter queue on exhaustion  ││
  └──► PostgreSQL state update ─────────┘│
       └──────────────────────────────────┘
```

## Features (v1 Milestone Plan)

| Milestone | Status | Description |
|-----------|--------|-------------|
| M1 | ✅ **Done** | Project skeleton, infra, API & worker skeletons |
| M2 | 🔲 Planned | Job submission, idempotency, Redis enqueue |
| M3 | 🔲 Planned | Worker lease acquisition, handler execution |
| M4 | 🔲 Planned | Retry backoff + dead-letter queue |
| M5 | 🔲 Planned | Tests, observability, production hardening |

## Tech Stack

- **API**: FastAPI + uvicorn (async)
- **DB**: PostgreSQL 16 via SQLAlchemy 2 (asyncpg)
- **Queue**: Redis 7 (BLPOP-based, no polling busy-loop)
- **Migrations**: Alembic (async-aware)
- **Logging**: structlog (JSON output)
- **Runtime**: Python 3.12

## Quick Start

### Prerequisites

- Docker ≥ 24
- Docker Compose v2 (`docker compose`)

### Run Locally

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd atlasqueue

# 2. Copy environment template (pre-filled for local dev)
cp .env.example .env.docker

# 3. Build images and start all services
docker compose up --build

# 4. Verify health
curl -s http://localhost:8000/health | python3 -m json.tool
# → {"status": "ok", "service": "atlasqueue-api"}

# 5. Inspect DB schema
docker compose exec postgres psql -U atlasqueue -d atlasqueue -c "\d jobs"

# 6. Tail worker logs (should show JSON polling events)
docker compose logs worker -f
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| api | 8000 | FastAPI REST API |
| postgres | 5432 | PostgreSQL 16 |
| redis | 6379 | Redis 7 |
| migrator | — | Runs `alembic upgrade head` once then exits |
| worker | — | Job consumer (BLPOP loop) |

### Stop

```bash
docker compose down          # keep DB data
docker compose down -v       # also delete volumes
```

## Project Structure

```
atlasqueue/
├── atlasqueue/
│   ├── core/
│   │   ├── config.py      # pydantic-settings — all env vars
│   │   ├── enums.py       # JobStatus enum
│   │   └── logging.py     # structlog JSON configuration
│   ├── db/
│   │   ├── models.py      # SQLAlchemy ORM (Job, IdempotencyKey)
│   │   └── session.py     # async engine + session factory
│   ├── api/
│   │   ├── main.py        # FastAPI app factory + lifespan
│   │   ├── deps.py        # Dependency injection (DB, Redis)
│   │   ├── schemas.py     # Pydantic I/O models
│   │   └── routers/
│   │       ├── health.py  # GET /health
│   │       └── jobs.py    # POST /v1/jobs, GET /v1/jobs/{id}
│   └── worker/
│       ├── consumer.py    # BLPOP polling loop
│       └── main.py        # Worker entry point
├── migrations/
│   ├── versions/
│   │   └── 0001_initial.py  # Creates jobs + idempotency_keys tables
│   ├── env.py             # Async Alembic environment
│   └── script.py.mako
├── requirements/
│   ├── base.txt
│   ├── api.txt
│   ├── worker.txt
│   └── dev.txt
├── Dockerfile.api
├── Dockerfile.worker
├── docker-compose.yml
├── alembic.ini
├── pyproject.toml
└── .env.example
```

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async Postgres DSN |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `WORKER_LEASE_SECONDS` | `30` | Job lock timeout |
| `RETRY_BASE_DELAY_SECONDS` | `5` | Backoff base (PRD §9) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Job State Machine

```
queued ──► running ──► succeeded
              │
              ▼
           failed ──► queued  (retry, attempts < max_attempts)
              │
              ▼
            dead          (DLQ, attempts exhausted)
```

## Retry Policy

```
delay = base_delay * (2 ^ attempts) + random(0, jitter_max)
```

Default: `base_delay=5s`, `jitter_max=2s`

## Contributing

See the milestone plan above. Each milestone is an incremental, independently committable unit of work.
