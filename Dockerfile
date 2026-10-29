# Institutional Binance 24/7 Autonomous Crypto Trader & Live Web Dashboard
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and writing pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8765

WORKDIR /app

# Install curl for Docker healthcheck and system maintenance
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Ensure storage directory exists
RUN mkdir -p /app/trader/live

# Expose web dashboard port
EXPOSE 8765

# Container health check (Docker & cloud orchestration)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8765}/health || exit 1

# Launch 24/7 Watchdog Supervisor
CMD ["python", "trader/live/watchdog.py"]
