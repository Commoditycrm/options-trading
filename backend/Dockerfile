# Demo backend image for the queue-based fanout (anitha-queue-demo).
# Runs migrations then the FastAPI app (uvicorn) — the asyncio worker pool
# starts inside this same process via the startup hook in app/main.py.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# No apt/build-essential: every dependency (psycopg[binary], cryptography,
# bcrypt, pandas via webull, snaptrade-python-sdk, …) ships prebuilt
# manylinux wheels for linux/amd64, so pip needs no compiler. This keeps the
# image slim and avoids the Debian-mirror DNS dependency at build time.
COPY requirements.txt .
# --timeout/--retries: the webull SDK pulls large transitive wheels (pandas),
# and slow links can ReadTimeout on the default 15s. Be patient + retry.
RUN pip install --timeout 120 --retries 5 -r requirements.txt

COPY . .

EXPOSE 8000

# alembic first; if migrations fail, uvicorn never starts (container shows
# unhealthy) — same fail-closed behaviour as the Render blueprint.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
