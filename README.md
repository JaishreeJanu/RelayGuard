# RelayGuard

RelayGuard is a fault-tolerant notification gateway. Clients submit delivery requests over HTTP; the API accepts them immediately, persists state in PostgreSQL, and hands work to a background worker. The worker delivers messages to a downstream vendor API with retries, a circuit breaker, and a dead-letter queue when delivery is no longer recoverable.

Built to handle unreliable third-party services without losing requests or double-sending them.

## What it does

| Capability | How it works |
|---|---|
| **Async ingestion** | `POST /api/v1/notifications/notify` returns `202` right away. Delivery runs in the background. |
| **Idempotency** | Each request carries an `idempotency_key`. Redis blocks duplicates for 5 minutes; PostgreSQL enforces uniqueness permanently. |
| **Retries** | Failed deliveries retry up to 3 times with exponential backoff and jitter. |
| **Circuit breaker** | After 3 consecutive vendor failures, outbound calls pause for 60 seconds to protect the system. |
| **Dead letter queue (DLQ)** | Notifications that exhaust all retries move to `DLQ` for manual review. |
| **Reconciliation** | `POST /api/v1/notifications/requeue-backlog` re-enqueues `FAILED` rows (and optionally `DLQ` rows) after an outage. |
| **Observability** | A React dashboard shows circuit breaker state, queue metrics, and a live delivery log. |
| **Chaos testing** | A mock vendor simulates healthy responses, HTTP 500 errors, and latency storms. |

## Architecture

```
Client → FastAPI (backend) → PostgreSQL
              ↓
           Redis queue (ARQ)
              ↓
         Worker → Mock vendor API
              ↓
         Redis (circuit breaker + idempotency)
```

**Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, PostgreSQL, Redis, ARQ, React (Vite), Nginx.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/v1/notifications/notify` | Submit a notification |
| `GET` | `/api/v1/notifications/status` | System telemetry for the dashboard |
| `POST` | `/api/v1/notifications/requeue-backlog` | Flush failed/DLQ notifications back to the queue |
| `GET` | `/health` | Health check |

**Example request:**

```bash
curl -X POST http://localhost:8000/api/v1/notifications/notify \
  -H "Content-Type: application/json" \
  -d '{
    "recipient": "user@example.com",
    "payload": {"template_id": "welcome", "name": "Alex"},
    "idempotency_key": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
  }'
```

Duplicate `idempotency_key` values return `409 Conflict`.

## Local development

**1. Start infrastructure**

```bash
cp .env.example .env
docker compose up -d postgres redis
```

**2. Run database migrations**

```bash
pip install -r requirements.txt
alembic upgrade head
```

**3. Start the services** (separate terminals)

```bash
uvicorn app.main:app --reload --port 8000          # API
arq app.worker.WorkerSettings                       # Worker
uvicorn mock_vendor.main:app --reload --port 8001   # Mock vendor
cd frontend && npm install && npm run dev           # Dashboard (port 5173)
```

**4. Run tests**

```bash
pytest
```

## Production deployment

The full stack is defined in `docker-compose.prod.yml`:

- `postgres`, `redis` — data stores
- `backend` — API (runs migrations on startup)
- `worker` — background delivery
- `mock_vendor` — downstream API simulator
- `frontend` — Nginx-served dashboard

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Set these environment variables before deploying:

| Variable | Description |
|---|---|
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |
| `POSTGRES_DB` | Database name |
| `DATABASE_URL` | PostgreSQL connection string (use service hostname `postgres`, not `localhost`) |
| `REDIS_URL` | Redis connection string (e.g. `redis://redis:6379/0`) |
| `VITE_API_URL` | Public URL of the backend (for frontend build) |
| `VITE_VENDOR_URL` | Public URL of the mock vendor (for frontend build) |

For [Coolify](https://coolify.io) deployments, assign domains to the three public services via the magic env vars already in the compose file: `SERVICE_URL_FRONTEND_80`, `SERVICE_URL_BACKEND_8000`, and `SERVICE_URL_MOCK_VENDOR_8001`.

## Notification lifecycle

```
PENDING → SENT          (delivery succeeded)
PENDING → FAILED → SENT (succeeded after retry)
PENDING → FAILED → DLQ  (max retries exhausted)
```

## Design decisions

These choices trade simplicity for reliability. They are intentional, not accidental.

**Accept fast, deliver slow.** The API returns `202 Accepted` as soon as the record is persisted and enqueued. Clients are not blocked by vendor latency or retries. PostgreSQL is the source of truth; Redis coordinates queues, locks, and circuit state.

**Two-layer idempotency.** Redis (`SET NX` with a 5-minute TTL) stops duplicate requests at the edge with minimal latency. PostgreSQL enforces a unique constraint on `idempotency_key` permanently. If the Redis lock expires but the row still exists, the API returns the existing record instead of failing or double-enqueueing.

**Worker reads fresh state every time.** Before calling the vendor, the worker reloads the notification from PostgreSQL. This prevents stale queue messages from re-sending completed deliveries or racing with reconciliation.

**Circuit breaker lives in Redis.** After 3 consecutive vendor failures, the breaker opens for 60 seconds. All workers share this state. While open, jobs are deferred (not dropped) so nothing is lost during an outage.

**Retries are bounded and spaced out.** Maximum 3 delivery attempts. Backoff is exponential with ±20% jitter to avoid thundering herds when the vendor recovers.

**DLQ is explicit, not automatic recovery.** Exhausted retries land in `DLQ`. Reconciliation re-enqueues `FAILED` rows by default. `DLQ` rows require an operator to opt in via `include_dlq=true` — this prevents poison-pill messages from looping forever.

**API and worker are separate processes.** The API handles ingestion and observability. The worker handles outbound HTTP. They scale independently and fail independently.

**Outbound calls have a hard timeout.** The worker aborts vendor requests after 5 seconds. Without this, a slow vendor can tie up worker slots and stall the entire queue.

## Failure scenarios

What happens when things go wrong — and what the system does about it.

| Scenario | System behavior |
|---|---|
| **Duplicate `idempotency_key`** | Second request gets `409 Conflict`. Queue is not touched. Redis counter `idempotency:blocked_count` increments. |
| **Invalid request body** | Pydantic rejects at the API boundary with `422`. Nothing is written to the database. |
| **Vendor returns HTTP 500** | Status set to `FAILED`. Retry scheduled with exponential backoff + jitter. Failure counter in Redis increments. |
| **Vendor times out (> 5s)** | Treated as a delivery failure. Same retry path as a 500. |
| **3 consecutive vendor failures** | Circuit breaker opens for 60 seconds. In-flight jobs are deferred 15 seconds and retried later. |
| **Circuit breaker is open** | Worker skips the vendor call and raises `Retry(defer=15)`. Notification stays in its current DB status until the circuit closes. |
| **Vendor recovers** | Successful delivery clears the failure counter. Breaker TTL expires automatically after 60 seconds. |
| **All 3 retries exhausted** | Status moves to `DLQ`. No further automatic delivery attempts. |
| **Queue message for missing DB row** | Worker logs the error and exits cleanly. No infinite retry loop on ghost records. |
| **Queue message for already-sent notification** | Worker detects `SENT` or `DLQ` status and skips processing. |
| **Operator flushes backlog after outage** | `POST /requeue-backlog` resets `FAILED` rows to `PENDING` and re-enqueues them. Optional `include_dlq=true` also resurrects dead-letter records. |

**Example: vendor outage end-to-end**

```
1. Vendor starts returning 500
2. Notifications move to FAILED, retries begin with backoff
3. After 3 failures → circuit breaker OPEN
4. Worker defers jobs for 15s while circuit is open
5. After 60s → circuit auto-closes (Redis TTL)
6. Operator confirms vendor is healthy
7. POST /requeue-backlog → stuck FAILED rows re-enter the queue
8. Deliveries succeed → status becomes SENT
```

## API response schemas

### `POST /api/v1/notifications/notify`

**Request body:**

```json
{
  "recipient": "user@example.com",
  "payload": { "template_id": "welcome", "name": "Alex" },
  "idempotency_key": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
}
```

| Field | Type | Constraints |
|---|---|---|
| `recipient` | string | Required, 1–512 chars |
| `payload` | object | Required, arbitrary JSON |
| `idempotency_key` | string | Required, 1–255 chars, unique |

**Response `202 Accepted`:**

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "recipient": "user@example.com",
  "payload": { "template_id": "welcome", "name": "Alex" },
  "status": "PENDING",
  "retry_count": 0,
  "idempotency_key": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "created_at": "2026-06-05T12:00:00Z",
  "updated_at": "2026-06-05T12:00:00Z"
}
```

**Error responses:**

| Status | When | Body |
|---|---|---|
| `409 Conflict` | Duplicate `idempotency_key` | `{ "detail": "Duplicate request detected. A transaction with idempotency key '...' is already being processed or completed." }` |
| `422 Unprocessable Entity` | Missing or invalid fields | Pydantic validation error with field names |

---

### `GET /api/v1/notifications/status`

**Response `200 OK`:**

```json
{
  "circuit_breaker": {
    "state": "CLOSED",
    "consecutive_failures": 0
  },
  "idempotency": {
    "blocked_duplicates": 3
  },
  "database_metrics": {
    "PENDING": 2,
    "SENT": 45,
    "FAILED": 1,
    "DLQ": 0
  },
  "recent_notifications": [
    {
      "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "recipient": "user@example.com",
      "status": "SENT",
      "retry_count": 0,
      "idempotency_key": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    }
  ]
}
```

| Field | Description |
|---|---|
| `circuit_breaker.state` | `CLOSED` (normal) or `OPEN` (vendor calls paused) |
| `circuit_breaker.consecutive_failures` | Failures since last successful delivery |
| `idempotency.blocked_duplicates` | Total duplicate requests rejected at the edge |
| `database_metrics` | Count of notifications per status |
| `recent_notifications` | Last 10 records for the dashboard stream |

---

### `POST /api/v1/notifications/requeue-backlog`

**Query parameters:**

| Param | Default | Description |
|---|---|---|
| `include_dlq` | `false` | When `true`, also re-enqueues `DLQ` records |

**Response `200 OK`:**

```json
{
  "status": "success",
  "message": "Successfully identified and re-enqueued 2 notifications.",
  "scope_applied": ["FAILED"]
}
```

With `?include_dlq=true`, `scope_applied` includes `"DLQ"` and the message count reflects all matched rows.

---

### `GET /health`

**Response `200 OK`:**

```json
{ "status": "healthy" }
```

## Project layout

```
app/           FastAPI backend, worker, models, services
frontend/      React observability dashboard
mock_vendor/   Simulated downstream email API with chaos modes
alembic/       Database migrations
tests/         Integration tests (idempotency, ingestion, reconciliation, worker)
```
