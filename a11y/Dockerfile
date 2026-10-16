FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/a11y

# System deps for lxml builds (wheels usually cover this; kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install "x402[evm]==2.23.0"

# Service code + the shared audit lib (common/auditlib.py is the canonical
# source at ~/workspace/agent-api/common/; bundled here so each service is
# self-contained at deploy time).
COPY a11y/ ./a11y/
COPY common/ ./common/
COPY server.py .

EXPOSE 8000

# All payment config via env vars (see .env.example). No secrets baked in.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
