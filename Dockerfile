# ── CloudPulse Monitor Dockerfile ────────────────────────────────────────────
# Minimal Python 3.11 image. Deployable to IBM Cloud, Azure, AWS, GCP.
# Run as a sidecar container alongside any micro-service stack.

FROM python:3.11-slim

WORKDIR /app

# Layer caching: install deps before copying source
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Use --workers 1 for asyncio (single event loop handles all concurrency)
CMD ["uvicorn", "cloudpulse:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
