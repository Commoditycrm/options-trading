"""Standalone fanout-worker entrypoint.

Run as a separate service in production (Render background-worker, AWS
ECS task, etc.). For local dev the worker also runs inside the FastAPI
process when RUN_FANOUT_WORKER_IN_PROCESS=true.

Why standalone matters: scale the worker count independently of the
backend's memory budget. The HTTP server stays focused on requests +
SSE; the worker pool can be sized purely for the fanout throughput
target.

This launcher runs FANOUT_WORKER_COUNT workers in one Python process
via a ThreadPoolExecutor. Each worker pulls from the same Consumer
Group, so messages are split across them — true parallel processing.

Sizing guide (typical ~30-50MB per worker):
  Render Starter   512MB  ->  ~8 workers   ->  100 subs in ~7s
  Render Standard  2GB    ->  ~30 workers  ->  100 subs in ~2s
  Render Pro       4GB    ->  ~50 workers  ->  100 subs in ~1.5s
                                                (~500ms is the floor —
                                                 one broker REST call.)

Usage:
    python worker.py
"""
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor

# Make sure backend/app is importable when invoked from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from app.config import get_settings
from app.services.fanout_stream import consume_loop


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    log = logging.getLogger("worker")

    settings = get_settings()
    n = max(1, settings.fanout_worker_count)
    log.info("starting %d fanout worker thread(s)", n)

    try:
        # ThreadPoolExecutor lets us run N consume_loops concurrently in one
        # process. Each loop blocks on XREADGROUP independently; the pool
        # only releases when all threads exit (which only happens on
        # KeyboardInterrupt or an unhandled error in a loop).
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="fanout") as pool:
            futures = [pool.submit(consume_loop) for _ in range(n)]
            # Block indefinitely — workers are designed to run forever.
            for f in futures:
                f.result()
    except KeyboardInterrupt:
        log.info("received KeyboardInterrupt, shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
