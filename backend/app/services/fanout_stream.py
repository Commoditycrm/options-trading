"""Publish + consume helpers for the fan-out Redis Stream.

Why Streams (and not Pub/Sub)
-----------------------------
Pub/Sub broadcasts every message to every subscriber. If we ran two worker
processes both subscribed to a `fanout` channel, each subscriber order would
be mirrored TWICE — once per worker. Catastrophic.

Streams + Consumer Groups give us the opposite shape:
  - XADD writes a message to the stream (durable, ordered)
  - N workers in the same consumer group share the work — each message goes
    to exactly ONE worker (via XREADGROUP)
  - Worker XACK's the message after the work completes
  - If the worker dies before XACK, the message stays in the consumer
    group's Pending Entries List (PEL) and can be reclaimed by another
    worker via XCLAIM after an idle timeout

This module is the small shared layer for both ends:
  - ``publish_targets()`` is called from the trader-detection path
    (alpaca_stream.py + trades.py's async submit) — one XADD per
    (trader_order, subscriber, broker_account)
  - ``consume_loop()`` is the worker body — XREADGROUP, dispatch to
    copy_engine.process_one_fanout, XACK

Granularity is one message per (trader_order × subscriber ×
broker_account). Most subscribers have one broker account so that's
effectively one message per (trader_order × subscriber). Fine-grained
acks: if subscriber A's mirror succeeds but subscriber B's broker is
down, only B's message stays in the PEL for retry.

Connection model
----------------
We use the sync `redis` client (not redis.asyncio) for simplicity — the
worker loop already lives in its own thread/process. The publish side
runs inside the alpaca_stream asyncio handler; XADD is fast (~1ms over
LAN, ~5ms over Internet) so calling it from an async context is fine
even without awaiting.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Iterable

from app.config import get_settings
from app.services.copy_engine import FanoutTarget, process_one_fanout

log = logging.getLogger(__name__)

# Stream key + group are settings-driven so deployments can isolate
# environments (e.g. dev / staging both pointing at the same Redis).
_REDIS = None  # lazy: connection built on first call


def _client():
    """Return the singleton redis client. Lazy to make module import cheap
    when Redis isn't configured."""
    global _REDIS
    if _REDIS is not None:
        return _REDIS
    s = get_settings()
    if not s.redis_url:
        raise RuntimeError(
            "REDIS_URL is not set. Either configure Redis or leave the "
            "fanout in-process (set RUN_FANOUT_WORKER_IN_PROCESS=false "
            "and skip publishing to the stream)."
        )
    import redis  # local import — keep optional
    _REDIS = redis.from_url(s.redis_url, decode_responses=True)
    return _REDIS


def is_configured() -> bool:
    """True if REDIS_URL is set. Callers use this to decide whether to
    publish-via-Redis or fall back to in-process fanout."""
    return bool(get_settings().redis_url)


# ─── Publish side ───────────────────────────────────────────────────────────

def publish_targets(trader_order_id: uuid.UUID, targets: Iterable[FanoutTarget]) -> int:
    """XADD one message per target. Returns the count published.

    Schema of each message (fields are stringified — Streams store strings):
      trader_order_id      str(uuid)
      subscriber_user_id   str(uuid)
      broker_account_id    str(uuid)
      enqueued_at          ISO timestamp (informational; aids debugging)
    """
    s = get_settings()
    r = _client()
    count = 0
    for t in targets:
        r.xadd(
            s.fanout_stream,
            {
                "trader_order_id": str(trader_order_id),
                "subscriber_user_id": str(t.subscriber_user_id),
                "broker_account_id": str(t.broker_account_id),
                "enqueued_at": str(time.time()),
            },
        )
        count += 1
    log.info(
        "fanout_stream: published %d target(s) for trader_order=%s",
        count, trader_order_id,
    )
    return count


# ─── Consume side ───────────────────────────────────────────────────────────

@dataclass
class WorkerStats:
    processed: int = 0
    errors: int = 0
    skipped: int = 0
    reclaimed: int = 0


def _ensure_group() -> None:
    """Create the consumer group if it doesn't exist. Idempotent."""
    s = get_settings()
    r = _client()
    try:
        # mkstream=True creates the stream too if it doesn't exist yet —
        # otherwise xgroup_create errors against a non-existent stream.
        r.xgroup_create(s.fanout_stream, s.fanout_group, id="0", mkstream=True)
        log.info(
            "fanout_stream: created consumer group %s on stream %s",
            s.fanout_group, s.fanout_stream,
        )
    except Exception as exc:  # noqa: BLE001 — redis.exceptions.ResponseError on dup
        # "BUSYGROUP Consumer Group name already exists" → expected on every
        # boot after the first. Anything else, surface as a warning but
        # don't crash; the XREADGROUP call will produce a real error if the
        # group truly isn't usable.
        msg = str(exc)
        if "BUSYGROUP" in msg:
            return
        log.warning("fanout_stream: xgroup_create returned %s", msg)


def _claim_stuck(consumer_name: str, idle_ms: int = 60_000) -> list[tuple]:
    """Reclaim messages from dead workers' PELs.

    XPENDING tells us which messages are sitting unacknowledged. XCLAIM
    transfers ownership to us so XREADGROUP starts delivering them. We
    only claim messages idle >= idle_ms so we don't steal in-flight work
    from healthy workers.
    """
    s = get_settings()
    r = _client()
    try:
        # autoclaim simplifies the pending → claim flow in one call
        # (redis 6.2+). Returns (next_id, claimed_messages).
        _, claimed = r.xautoclaim(
            s.fanout_stream, s.fanout_group, consumer_name,
            min_idle_time=idle_ms, count=50,
        )
        return claimed or []
    except Exception:  # noqa: BLE001
        # XAUTOCLAIM unavailable on Redis < 6.2 — caller can poll XPENDING
        # + XCLAIM manually if needed. For now we just skip stuck recovery
        # on older Redis (Upstash/Render Key-Value both support 6.2+).
        return []


def _dispatch(msg_id: str, fields: dict, stats: WorkerStats) -> bool:
    """Run one fanout. Returns True if the message should be XACK'd."""
    try:
        trader_order_id = uuid.UUID(fields["trader_order_id"])
        target = FanoutTarget(
            subscriber_user_id=uuid.UUID(fields["subscriber_user_id"]),
            broker_account_id=uuid.UUID(fields["broker_account_id"]),
        )
    except (KeyError, ValueError) as exc:
        # Malformed message — log and ACK so it doesn't sit in PEL forever.
        # If the schema ever changes, this code surfaces the mismatch fast.
        log.error("fanout_stream: malformed message %s: %s", msg_id, exc)
        stats.errors += 1
        return True

    result = process_one_fanout(trader_order_id, target)
    if result.status == "submitted":
        stats.processed += 1
    elif result.status.startswith("skipped"):
        stats.skipped += 1
    else:
        stats.errors += 1
    # ACK on success AND on skip/error — process_one_fanout already
    # captured the failure into the audit log + child Order row. Leaving
    # the message in PEL would cause infinite retry of permanent failures.
    return True


def consume_loop(consumer_name: str | None = None, *, shutdown_check=None) -> WorkerStats:
    """Worker entry point. Blocks indefinitely consuming messages.

    consumer_name: identifier for this worker instance — visible in
        XINFO CONSUMERS. Defaults to a random uuid so multiple instances
        on the same host don't collide.

    shutdown_check: optional callable returning True when the loop should
        exit (used by tests / graceful shutdown). The loop also exits on
        KeyboardInterrupt.

    Returns the accumulated WorkerStats when the loop exits.
    """
    s = get_settings()
    if not s.redis_url:
        log.warning("fanout_stream.consume_loop: REDIS_URL not set, worker exiting")
        return WorkerStats()

    if consumer_name is None:
        consumer_name = f"worker-{uuid.uuid4().hex[:8]}"

    _ensure_group()
    r = _client()
    stats = WorkerStats()
    last_claim = 0.0
    log.info(
        "fanout_stream: consumer %s starting (stream=%s group=%s)",
        consumer_name, s.fanout_stream, s.fanout_group,
    )

    while True:
        if shutdown_check is not None and shutdown_check():
            log.info("fanout_stream: shutdown_check returned True, exiting")
            break

        try:
            # Block up to 5s waiting for new messages. count=10 lets one
            # XREADGROUP pull a small batch — the worker still processes
            # them serially (one broker call per loop iteration).
            resp = r.xreadgroup(
                s.fanout_group, consumer_name,
                {s.fanout_stream: ">"},
                count=10, block=5000,
            )
        except KeyboardInterrupt:
            log.info("fanout_stream: KeyboardInterrupt, exiting")
            break
        except Exception:  # noqa: BLE001
            log.exception("fanout_stream: XREADGROUP error, sleeping 5s")
            time.sleep(5)
            continue

        if resp:
            # resp shape: [(stream_name, [(msg_id, {fields}), ...])]
            for _stream, entries in resp:
                ack_ids = []
                for msg_id, fields in entries:
                    if _dispatch(msg_id, fields, stats):
                        ack_ids.append(msg_id)
                if ack_ids:
                    try:
                        r.xack(s.fanout_stream, s.fanout_group, *ack_ids)
                    except Exception:  # noqa: BLE001
                        log.exception("fanout_stream: XACK failed")

        # Periodically reclaim stuck messages from dead workers.
        # Every ~60s of wall clock, regardless of how busy we are.
        now = time.time()
        if now - last_claim > 60:
            claimed = _claim_stuck(consumer_name)
            if claimed:
                stats.reclaimed += len(claimed)
                log.info("fanout_stream: reclaimed %d stuck message(s)", len(claimed))
            last_claim = now

    log.info(
        "fanout_stream: consumer %s exiting (processed=%d skipped=%d errors=%d reclaimed=%d)",
        consumer_name, stats.processed, stats.skipped, stats.errors, stats.reclaimed,
    )
    return stats
