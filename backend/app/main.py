import asyncio
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    admin, auth, brokers, events, marketdata, notifications, options, positions, settings,
    solo, subscribers, trades, watchlist,
)
from app.config import get_settings
from app.services import (
    alpaca_stream, external_trade_poller, fanout_stream, listeners, memory_cache,
    position_monitor, retry_scheduler, subscriber_worker,
)
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
    app.include_router(notifications.router)
    app.include_router(admin.router)
    app.include_router(watchlist.router)
    app.include_router(marketdata.router)
    app.include_router(solo.router)

    @app.on_event("startup")
    async def _bind_loop() -> None:
        loop = asyncio.get_running_loop()
        events_bus.bind_loop(loop)
        # Bind the loop for the Webull/SnapTrade polling listeners too.
        listeners.bind_loop(loop)

    @app.on_event("startup")
    async def _start_broker_listeners() -> None:
        # Spawn Webull + SnapTrade detection listeners for every connected
        # trader account. (Alpaca is handled by _start_alpaca_streams below.)
        # Detected orders flow into copy_engine.dispatch_detected_order →
        # the queue fast path. Best-effort: missing SDKs are skipped.
        try:
            await listeners.start_all_listeners()
        except Exception:  # noqa: BLE001
            logging.getLogger("uvicorn").exception("failed to start broker listeners")

    @app.on_event("shutdown")
    async def _stop_broker_listeners() -> None:
        try:
            await listeners.stop_all_listeners()
        except Exception:  # noqa: BLE001
            pass

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

    @app.on_event("startup")
    async def _start_retry_scheduler() -> None:
        # Background loop that re-attempts subscriber mirror orders that
        # failed with a transient broker-disconnect error. Picks up rows
        # where status=RETRY_PENDING + retry_at <= now() and tries once.
        # See services/retry_scheduler.py for details.
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, retry_scheduler.poll_loop)
        logging.getLogger("uvicorn").info(
            "started retry_scheduler (interval=%ss)",
            retry_scheduler.POLL_INTERVAL_SEC,
        )

    @app.on_event("startup")
    async def _start_position_monitor() -> None:
        # Watches open positions that have a stop-loss / take-profit rule and
        # auto-closes them when the threshold price is crossed. Same poller
        # model as retry_scheduler (one thread-pool loop); polls only accounts
        # with an active rule. See services/position_monitor.py.
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, position_monitor.poll_loop)
        logging.getLogger("uvicorn").info(
            "started position_monitor (interval=%ss)",
            position_monitor.POLL_INTERVAL_SEC,
        )

    @app.on_event("startup")
    async def _start_queue_demo_workers() -> None:
        # Demo queue-based fanout architecture (anitha-queue-demo branch).
        # Load the subscriber cache into memory, then start a pool of
        # asyncio workers that drain pending_copies in parallel.
        # Env var: QUEUE_DEMO_WORKER_COUNT (default 100). Set to 0 to disable.
        count = int(os.environ.get("QUEUE_DEMO_WORKER_COUNT", "100"))
        if count <= 0:
            return
        try:
            memory_cache.load_all()
        except Exception:  # noqa: BLE001
            logging.getLogger("uvicorn").exception(
                "memory_cache.load_all failed; queue demo disabled",
            )
            return
        await subscriber_worker.start_workers(count=count)

    @app.on_event("startup")
    async def _prewarm_broker_clients() -> None:
        # Warm the cached broker HTTP/TLS connections so the FIRST order after a
        # restart doesn't pay the ~1.5s cold handshake (the cause of the high
        # "first trade after deploy" times). Best-effort, off the boot path.
        def _warm() -> int:
            from sqlalchemy import select as _select
            from app.database import SessionLocal as _S
            from app.models.broker_account import BrokerAccount as _BA, BrokerName as _BN
            from app.brokers import adapter_for as _adapter_for
            from app.services.crypto import decrypt_json as _decrypt
            log = logging.getLogger("uvicorn")
            warmed = 0
            try:
                with _S() as db:
                    accts = db.execute(_select(_BA).where(
                        _BA.connection_status == "connected",
                        _BA.broker == _BN.ALPACA,
                    )).scalars().all()
                for acct in accts:
                    try:
                        adapter = _adapter_for(acct, _decrypt(acct.encrypted_credentials))
                        adapter.get_balance_snapshot()  # one call warms the pooled TLS conn
                        warmed += 1
                    except Exception:  # noqa: BLE001
                        log.warning("prewarm: failed for broker_account=%s", acct.id)
            except Exception:  # noqa: BLE001
                log.exception("prewarm: sweep failed")
            log.info("prewarm: warmed %d broker client(s)", warmed)
            return warmed
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _warm)

    @app.get("/api/admin/demo/stats")
    def demo_stats(parent_order_id: str | None = None) -> dict:
        # Surfaces the queue-demo timing data for the admin dashboard.
        # Without parent_order_id: returns the most recent batch.
        from sqlalchemy import desc, select as _select
        from app.database import SessionLocal as _S
        from app.models.pending_copy import PendingCopy as _PC
        with _S() as db:
            if parent_order_id is None:
                latest = db.execute(
                    _select(_PC.parent_order_id)
                    .order_by(desc(_PC.queued_at)).limit(1)
                ).scalar()
                if latest is None:
                    return {"parent_order_id": None, "rows": [],
                            "memory_cache": memory_cache.snapshot_stats()}
                parent_order_id = str(latest)
            rows = db.execute(
                _select(_PC).where(_PC.parent_order_id == parent_order_id)
                .order_by(_PC.queued_at)
            ).scalars().all()
            return {
                "parent_order_id": parent_order_id,
                "memory_cache": memory_cache.snapshot_stats(),
                "worker_heartbeat": subscriber_worker.heartbeat_status(),
                "rows": [{
                    "id": str(r.id),
                    "subscriber_user_id": str(r.subscriber_user_id),
                    "status": r.status.value,
                    "queued_at": r.queued_at.isoformat() if r.queued_at else None,
                    "picked_up_at": r.picked_up_at.isoformat() if r.picked_up_at else None,
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                    "queue_to_broker_ms": r.queue_to_broker_ms,
                    "pickup_ms": r.pickup_ms,
                    "platform_ms": r.platform_ms,
                    "detail": r.detail,
                } for r in rows],
            }

    @app.get("/api/config")
    def public_config() -> dict:
        # PUBLIC (unauthenticated) white-label config for the frontend. The
        # login/register pages render the brand name before the user has a
        # token, so this must not require auth. Falls back to the default if
        # the single config row is somehow missing.
        from app.database import SessionLocal as _S
        from app.models.app_config import AppConfig as _AC
        with _S() as db:
            cfg = db.get(_AC, 1)
            return {"business_name": cfg.business_name if cfg else "The Option Haven"}

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "disclaimer": DISCLAIMER,
            # Operational visibility — confirms the retry_scheduler thread
            # is still ticking. "healthy" flips false if the loop hasn't
            # run within 3 poll intervals (~30s default).
            "retry_scheduler": retry_scheduler.heartbeat_status(),
            "position_monitor": position_monitor.heartbeat_status(),
            "external_trade_poller": external_trade_poller.heartbeat_status(),
            # Fan-out worker pool + the LISTEN/NOTIFY wake-up that drives the
            # <50ms platform pickup. queue_listener.healthy == instant wake-ups;
            # if false, workers fall back to the slower poll (still correct).
            "subscriber_worker": subscriber_worker.heartbeat_status(),
            "queue_listener": subscriber_worker.listener_status(),
        }

    return app


app = create_app()
