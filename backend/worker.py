"""Standalone fanout-worker entrypoint.

Run as a separate service in production (Render background-worker, AWS
ECS task, etc.). For local dev the worker also runs inside the FastAPI
process when RUN_FANOUT_WORKER_IN_PROCESS=true (default in .env.example).

Why standalone matters: scale the worker count independently of the
backend. If you have 200 subscribers per trade and want sub-second
fanout, run 20 worker instances; each grabs ~10 messages and finishes
in ~2 seconds of broker-call time. The backend stays a single API
process.

Usage:
    python worker.py
"""
import logging
import os
import sys

# Make sure backend/app is importable when invoked from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from app.services.fanout_stream import consume_loop


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger("worker")
    log.info("starting fanout worker")
    try:
        consume_loop()
    except KeyboardInterrupt:
        log.info("received KeyboardInterrupt, shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
