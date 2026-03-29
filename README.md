# CloudPulse Monitor

An asynchronous client-server health monitoring service. Registers HTTP endpoints, polls them concurrently via asyncio, maintains a rolling 100-check history per service, and detects anomalies (consecutive failures, latency spikes) through a REST API.

## Architecture

```
POST /services  →  Register endpoint + start async poll loop
                          ↓
              asyncio event loop (single-threaded, non-blocking)
              polls N services concurrently — no thread overhead
                          ↓
              Rolling deque (100 checks/service, FIFO)
                          ↓
              Anomaly detector:
                • 3 consecutive failures → CRITICAL
                • Latency > 2x rolling avg → WARNING
                • Uptime < 95% → WARNING
                          ↓
GET /dashboard  →  System-wide health summary + active alerts
```

## Quickstart

```bash
pip install -r requirements.txt
uvicorn cloudpulse:app --reload
```

## Docker

```bash
docker build -t cloudpulse .
docker run -p 8000:8000 cloudpulse
```

## Register a Service

```bash
curl -X POST http://localhost:8000/services \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Auth Service",
    "url": "https://your-service.com/health",
    "interval_seconds": 30,
    "timeout_seconds": 5
  }'
```

## Dashboard Response

```json
{
  "summary": {
    "total": 3, "healthy": 2, "degraded": 1, "down": 0,
    "overall_health": "degraded"
  },
  "active_alerts": [
    "WARNING: Latency spike — 842ms vs avg 210ms."
  ],
  "services": [...]
}
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/services` | Register endpoint for monitoring |
| GET | `/services` | All services + health status |
| GET | `/services/{id}` | Single service status |
| GET | `/services/{id}/history` | Last N check results |
| DELETE | `/services/{id}` | Stop monitoring |
| GET | `/dashboard` | Full system health overview |

## Design Notes
- **asyncio over threads** — single event loop handles N concurrent pollers with zero thread overhead
- **Rolling deque** — O(1) append/drop, bounded memory regardless of uptime
- **Swappable storage** — replace in-memory dicts with Redis for persistence and multi-instance support

## Stack
Python 3.11 · FastAPI · asyncio · httpx · Docker
