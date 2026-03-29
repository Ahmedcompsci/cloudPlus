"""
CloudPulse Monitor — Async Distributed Service Health Monitor
=============================================================
A client-server monitoring system built on FastAPI and asyncio.

What it does:
    - Accepts HTTP service endpoint registrations via REST API
    - Polls each endpoint asynchronously at a configurable interval
    - Tracks a rolling 100-check history per service (uptime, latency)
    - Detects anomalies: consecutive failures, latency spikes
    - Exposes a dashboard endpoint summarizing overall system health

Why asyncio:
    Polling N services concurrently with threads would require N threads —
    expensive and doesn't scale. asyncio uses a single event loop with
    cooperative coroutines: zero thread overhead, handles N services in parallel.

Deployment:
    Runs as a standalone Docker container.
    Can be deployed as a sidecar alongside any cloud micro-service stack
    on AWS ECS, Azure Container Apps, IBM Cloud, or GCP Cloud Run.

Run locally:
    pip install -r requirements.txt
    uvicorn cloudpulse:app --reload

Docker:
    docker build -t cloudpulse .
    docker run -p 8000:8000 cloudpulse
"""

import asyncio
import time
import statistics
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="CloudPulse Monitor",
    description="Asynchronous distributed service health monitoring via REST API.",
    version="1.0.0",
)

# ── Constants ─────────────────────────────────────────────────────────────────

HISTORY_LIMIT       = 100   # max check results stored per service (rolling window)
ANOMALY_FAILURE_N   = 3     # consecutive failures before CRITICAL alert
ANOMALY_LATENCY_MUL = 2.0   # latency spike multiplier vs rolling average
UPTIME_THRESHOLD    = 95.0  # uptime % below which WARNING is raised

# ── In-memory state ───────────────────────────────────────────────────────────
# In production, replace with Redis or a time-series DB (InfluxDB, Prometheus).

services: Dict[str, dict]         = {}   # service_id → config + counters
history:  Dict[str, deque]        = {}   # service_id → rolling check history
polling:  Dict[str, bool]         = {}   # service_id → polling active flag


# ── Pydantic models ───────────────────────────────────────────────────────────

class ServiceConfig(BaseModel):
    """Configuration for a service to be monitored."""
    name:             str
    url:              str
    interval_seconds: int = 30   # how often to poll (seconds)
    timeout_seconds:  int = 5    # request timeout before marking as failure
    expected_status:  int = 200  # HTTP status code considered healthy


class ServiceStatus(BaseModel):
    """Computed health status for a single service."""
    id:             str
    name:           str
    url:            str
    status:         str           # 'healthy' | 'degraded' | 'down' | 'unknown'
    uptime_pct:     float
    avg_latency_ms: float
    last_checked:   Optional[str]
    check_count:    int
    failure_count:  int
    alerts:         List[str]


# ── Core polling logic ────────────────────────────────────────────────────────

async def check_once(service_id: str) -> dict:
    """
    Perform a single HTTP health check against a registered service.

    Returns a check result dict with:
        timestamp   — ISO 8601 UTC
        status_code — HTTP response code (None on network error)
        latency_ms  — round-trip time in milliseconds (None on error)
        success     — True if status_code matches expected_status
        error       — error message string (None on success)
    """
    svc = services[service_id]
    result = {
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "status_code": None,
        "latency_ms":  None,
        "success":     False,
        "error":       None,
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=svc["timeout_seconds"]) as client:
            resp = await client.get(svc["url"])
            result["status_code"] = resp.status_code
            result["latency_ms"]  = round((time.monotonic() - start) * 1000, 2)
            result["success"]     = (resp.status_code == svc["expected_status"])
    except httpx.TimeoutException:
        result["error"] = "timeout"
    except httpx.ConnectError:
        result["error"] = "connection_refused"
    except Exception as e:
        result["error"] = str(e)

    return result


async def poll_loop(service_id: str):
    """
    Continuously poll a service until polling[service_id] is set to False.

    Uses asyncio.sleep() — non-blocking, allows other services to poll
    concurrently on the same event loop without thread overhead.
    """
    polling[service_id] = True

    while polling.get(service_id, False):
        result = await check_once(service_id)

        # Append to rolling history (deque auto-drops oldest when at HISTORY_LIMIT)
        history[service_id].append(result)

        # Update counters on the service record
        services[service_id]["check_count"]  += 1
        services[service_id]["last_checked"]  = result["timestamp"]
        if not result["success"]:
            services[service_id]["failure_count"] += 1

        # Wait before next poll — asyncio.sleep yields control to event loop
        await asyncio.sleep(services[service_id]["interval_seconds"])


# ── Anomaly detection + status computation ────────────────────────────────────

def compute_status(service_id: str) -> ServiceStatus:
    """
    Derive a health status and alert list from the rolling check history.

    Status levels:
        healthy  — no active alerts
        degraded — at least one WARNING
        down     — at least one CRITICAL
        unknown  — no checks have run yet
    """
    svc    = services[service_id]
    checks = list(history[service_id])
    alerts = []

    # No data yet — service just registered
    if not checks:
        return ServiceStatus(
            id=service_id, name=svc["name"], url=svc["url"],
            status="unknown", uptime_pct=0.0, avg_latency_ms=0.0,
            last_checked=None, check_count=0, failure_count=0, alerts=[]
        )

    # ── Compute metrics ───────────────────────────────────────────────────────
    successes   = [c for c in checks if c["success"]]
    uptime_pct  = round(len(successes) / len(checks) * 100, 1)
    latencies   = [c["latency_ms"] for c in checks if c["latency_ms"] is not None]
    avg_latency = round(statistics.mean(latencies), 1) if latencies else 0.0

    # ── Anomaly checks ────────────────────────────────────────────────────────

    # CRITICAL: N consecutive failures
    if len(checks) >= ANOMALY_FAILURE_N:
        recent = checks[-ANOMALY_FAILURE_N:]
        if not any(c["success"] for c in recent):
            alerts.append(
                f"CRITICAL: {ANOMALY_FAILURE_N} consecutive failures detected."
            )

    # WARNING: latest latency > 2x rolling average
    if len(latencies) >= 5:
        rolling_avg = statistics.mean(latencies[:-1])
        if latencies[-1] > rolling_avg * ANOMALY_LATENCY_MUL:
            alerts.append(
                f"WARNING: Latency spike — {latencies[-1]}ms vs avg {round(rolling_avg, 1)}ms."
            )

    # WARNING: uptime below threshold
    if uptime_pct < UPTIME_THRESHOLD:
        alerts.append(f"WARNING: Uptime {uptime_pct}% is below {UPTIME_THRESHOLD}% threshold.")

    # ── Derive overall status ─────────────────────────────────────────────────
    if any("CRITICAL" in a for a in alerts):
        status = "down"
    elif alerts:
        status = "degraded"
    else:
        status = "healthy"

    return ServiceStatus(
        id=service_id, name=svc["name"], url=svc["url"],
        status=status, uptime_pct=uptime_pct, avg_latency_ms=avg_latency,
        last_checked=svc.get("last_checked"),
        check_count=svc["check_count"],
        failure_count=svc["failure_count"],
        alerts=alerts,
    )


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"service": "CloudPulse Monitor", "registered_services": len(services)}


@app.post("/services", status_code=201)
async def register_service(config: ServiceConfig, background_tasks: BackgroundTasks):
    """
    Register a new service endpoint for monitoring.
    Starts an async polling loop in the background immediately.
    """
    service_id = str(uuid.uuid4())[:8]

    # Store config + initial counters
    services[service_id] = {
        "name":             config.name,
        "url":              config.url,
        "interval_seconds": config.interval_seconds,
        "timeout_seconds":  config.timeout_seconds,
        "expected_status":  config.expected_status,
        "check_count":      0,
        "failure_count":    0,
        "last_checked":     None,
    }

    # Initialize rolling history deque (auto-drops oldest at HISTORY_LIMIT)
    history[service_id] = deque(maxlen=HISTORY_LIMIT)

    # Launch polling coroutine as a background task (non-blocking)
    background_tasks.add_task(poll_loop, service_id)

    return {
        "id":      service_id,
        "message": f"Monitoring '{config.name}' every {config.interval_seconds}s.",
    }


@app.get("/services", response_model=List[ServiceStatus])
def list_services():
    """Return computed health status for all registered services."""
    return [compute_status(sid) for sid in services]


@app.get("/services/{service_id}", response_model=ServiceStatus)
def get_service(service_id: str):
    """Return health status for a single service by ID."""
    if service_id not in services:
        raise HTTPException(status_code=404, detail="Service not found.")
    return compute_status(service_id)


@app.get("/services/{service_id}/history")
def get_history(service_id: str, limit: int = 20):
    """Return the last N check results for a service (default 20)."""
    if service_id not in history:
        raise HTTPException(status_code=404, detail="Service not found.")
    return {"history": list(history[service_id])[-limit:]}


@app.delete("/services/{service_id}", status_code=204)
def remove_service(service_id: str):
    """Stop monitoring a service and remove its data."""
    if service_id not in services:
        raise HTTPException(status_code=404, detail="Service not found.")
    polling[service_id] = False   # signals poll_loop coroutine to exit
    services.pop(service_id)
    history.pop(service_id)


@app.get("/dashboard")
def dashboard():
    """
    Full system health overview.
    Returns summary counts, active alerts, and per-service status.
    Designed as the data source for a monitoring dashboard UI.
    """
    statuses   = [compute_status(sid) for sid in services]
    all_alerts = [alert for s in statuses for alert in s.alerts]

    return {
        "summary": {
            "total":          len(statuses),
            "healthy":        sum(1 for s in statuses if s.status == "healthy"),
            "degraded":       sum(1 for s in statuses if s.status == "degraded"),
            "down":           sum(1 for s in statuses if s.status == "down"),
            "overall_health": (
                "critical" if any(s.status == "down"     for s in statuses) else
                "degraded" if any(s.status == "degraded" for s in statuses) else
                "healthy"
            ),
        },
        "active_alerts": all_alerts,
        "services":      statuses,
    }
