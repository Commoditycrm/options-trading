import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth, brokers, events, options, positions, settings, subscribers, trades
from app.config import get_settings
from app.services import alpaca_stream, external_trade_poller, fanout_stream
from app.services import events as events_bus

# Python's root logger defaults to WARNING, which silences every log.info()
# call in our modules (alpaca_stream, fanout_stream, copy_engine, etc.).
# Bump to INFO so Render's Logs tab shows the activity we actually want to
# see: stream connect/disconnect, external-trade detection, fanout dispatch,
# per-subscriber copy.submitted / copy.error events.
# Set LOG_LEVEL=DEBUG in the Render env for even more detail.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
# Quieten the noisier libraries — alpaca-py and httpx log at INFO with every
# HTTP call, which would drown out our application events.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("alpaca").setLevel(logging.WARNING)

DISCLAIMER = (
    "Educational software. Not investment advice. Copy trading involves substantial risk "
    "of loss. The platform operator may need to register as an investment adviser under "
    "applicable securities laws (e.g. US SEC/FINRA) before charging subscribers. "
    "Verify your regulatory obligations before going live."
)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Copy Trading Platform",
        version="0.2.0",
        description=DISCLAIMER,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins_list,
        # allow_origin_regex covers Vercel's per-deployment URLs (production
        # alias + previews). EventSource hits us directly cross-origin (it
        # bypasses the Next.js rewrite to avoid Vercel's edge timeout on
        # long-lived SSE), so the Origin header is the user's Vercel URL —
        # which changes per deploy. Regex avoids re-setting CORS_ORIGINS
        # every time Vercel creates a new preview URL.
        allow_origin_regex=s.cors_origin_regex or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(brokers.router)
    app.include_router(trades.router)
    app.include_router(settings.router)
    app.include_router(subscribers.router)
    app.include_router(events.router)
    app.include_router(options.router)
    app.include_router(positions.router)

    @app.on_event("startup")
    async def _bind_loop() -> None:
        events_bus.bind_loop(asyncio.get_running_loop())

    @app.on_event("startup")
    async def _start_alpaca_streams() -> None:
        # Real-time trade-update WebSocket per connected Alpaca account.
        # Fills land in the DB + SSE within ~100ms of execution instead of
        # waiting for the frontend to poll sync-fills.
        #
        # NOTE: as of alpaca-py 0.33, this stream silently fails to deliver
        # events in our background-thread context. external_trade_poller
        # below is the working fallback (REST polling every 2s). Both run
        # — they dedupe via broker_order_id, so whichever sees the trade
        # first wins.
        await alpaca_stream.start_all_streams()

    @app.on_event("startup")
    async def _start_external_trade_poller() -> None:
        # REST-polling fallback for external-trade detection. Hits Alpaca's
        # /v2/orders endpoint every 2 seconds for each trader account with
        # mirror_external_trades=True, picks up new orders that aren't
        # already in our DB, dispatches fanout via the same Redis Streams
        # path as the WebSocket would have.
        #
        # Latency: 1-2 seconds (vs ~100ms for working WebSocket).
        # Always-on so the platform doesn't depend on the WebSocket health.
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, external_trade_poller.poll_loop)
        import logging
        logging.getLogger("uvicorn").info(
            "started external_trade_poller (REST fallback for trade-update WS)",
        )

    @app.on_event("shutdown")
    async def _stop_alpaca_streams() -> None:
        await alpaca_stream.stop_all_streams()

    @app.on_event("startup")
    async def _start_fanout_workers() -> None:
        # Run N fanout workers as asyncio thread-pool tasks inside the
        # FastAPI process. Each worker pulls from the SAME Consumer Group,
        # so the queue is drained in parallel — true concurrent broker calls
        # rather than serial. Worker count is FANOUT_WORKER_COUNT (env var,
        # default 8).
        #
        # For 100+ subscribers and tight latency targets, swap to a
        # dedicated worker service: set RUN_FANOUT_WORKER_IN_PROCESS=false
        # here and uncomment the worker block in render.yaml. That lets you
        # bump worker count without sharing memory with the HTTP server.
        if not s.redis_url:
            return
        if not s.run_fanout_worker_in_process:
            return
        loop = asyncio.get_running_loop()
        for _ in range(s.fanout_worker_count):
            # consume_loop is blocking sync code → run each in a thread
            # executor so they don't starve the FastAPI event loop.
            loop.run_in_executor(None, fanout_stream.consume_loop)
        import logging
        logging.getLogger("uvicorn").info(
            "started %d in-process fanout worker(s)", s.fanout_worker_count,
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "disclaimer": DISCLAIMER}

    return app


app = create_app()
