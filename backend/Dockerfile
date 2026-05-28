# Demo backend image for the queue-based fanout (anitha-queue-demo).
# Runs migrations then the FastAPI app (uvicorn) — the asyncio worker pool
# starts inside this same process via the startup hook in app/main.py.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: psycopg/cryptography wheels are prebuilt, but build-essential
# keeps us safe if a transitive dep needs to compile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# alembic first; if migrations fail, uvicorn never starts (container shows
# unhealthy) — same fail-closed behaviour as the Render blueprint.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
