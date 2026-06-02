# Fan-out architecture: in-process vs. Redis Streams

> **DECISION (2026-05-20):** **Redis Streams + Consumer Groups** is implemented and lives behind the `REDIS_URL` env var. When `REDIS_URL` is unset the codebase silently falls back to the in-process ThreadPoolExecutor path documented below (still functional for local dev and very-small deploys). The decision came after the client shared a flow diagram showing the Redis-based dispatch model and we confirmed the technical concern: standard Pub/Sub would have broadcast every fanout message to every worker, causing N × duplicate mirror orders. **Streams + Consumer Groups is the correct Redis primitive for this** — each message goes to exactly one worker in the group.
>
> The rest of this doc explains the trade-off framework that informed the decision. Kept for historical context and for re-evaluating if the deployment shape ever changes.

---

This doc compares the in-process implementation (still default fallback) against the Redis-Streams architecture (now active when `REDIS_URL` is set). Both ship the same user-visible behaviour: trader places an order, every subscriber's broker receives it in parallel within milliseconds, every subscriber's UI updates without a refresh. The difference is what becomes possible **as the platform grows**.

---

## What's the same in both

| Concern | Status |
|---|---|
| Trader places → all subscribers' brokers fire in parallel | ✅ both |
| Per-subscriber config (multiplier, copy on/off, daily loss limit) read at fan-out time | ✅ both |
| Subscriber UIs update in real time, no refresh | ✅ both |
| Postgres as source of truth for orders, fills, audit log | ✅ both |
| One failed subscriber doesn't block the others | ✅ both |
| Daily loss limit + master kill switch enforced before each subscriber order | ✅ both |

The "fast and live, no delay" requirement is met by both designs. The user experience is identical.

---

## What's different

### Today's implementation (single-process, in-memory)

```
Trader API ─► FastAPI BackgroundTask
                  │
                  ├─► Broker call for the trader (with retry / recovery)
                  │
                  └─► copy_engine.fanout()
                       │
                       └─► ThreadPoolExecutor (max 32 workers)
                            ├─► Subscriber 1 broker call
                            ├─► Subscriber 2 broker call
                            └─► Subscriber N broker call
                  │
                  └─► SSE event bus (per-user asyncio.Queue, in-memory)
                       └─► Browsers receive updates
```

One Python process holds everything. The "parallelism" is OS threads inside that process. SSE notifications are an in-memory dict-of-queues.

### Redis Streams implementation (what we shipped)

```
Trader places direct at broker         Or trader uses our Trade Panel
   │                                       │
   ▼                                       ▼
[alpaca_stream.py detects "new"]      [api/trades.py async background task]
   │                                       │
   └────────── (both paths converge) ──────┘
                                       │
                          enumerate_fanout_targets(db, trader_id)
                                       │
                          XADD one message per (subscriber, broker_account)
                                       │
                                       ▼
                       [Redis Stream: optionhaven:fanout]
                                       │
                          XREADGROUP (consumer group: fanout_workers)
                                       │
              ┌────────────────────────┼────────────────────────┐
              ▼                        ▼                        ▼
        [Worker A]                [Worker B]                [Worker C]
   process_one_fanout()      process_one_fanout()      process_one_fanout()
   - gate checks                                       (each worker pulls
   - place mirror order                                 one message, ACKs
   - audit + SSE                                        after success)
   - XACK
```

The detection / trader-API process and the worker processes are separate (in production). They communicate through Redis Streams. SSE notifications still flow through the in-memory `services/events.py` event bus since they're per-user broadcasts that don't benefit from Streams' work-queue semantics — that's the only piece still single-process. If you go multi-pod backend later, that's the next thing to swap (Redis Pub/Sub IS the right tool for SSE, just not for fanout).

---

## Where they diverge in practice

### 1. Multi-pod / horizontal scaling

- **In-process:** The whole fanout happens inside whichever pod served the trader's request. A second pod knows nothing about the in-flight subscribers being processed in the first pod. SSE listeners on the second pod can't receive events published from the first pod. **One backend pod, hard limit.**
- **Redis pub/sub:** Any pod can publish, any worker can consume. SSE clients connect to whichever pod they land on; events flow through Redis. **Scales to N pods with no code change.**

### 2. Resilience to mid-fanout crash

- **In-process:** If the backend process restarts in the middle of fanning out to 200 subscribers, the half that hadn't been picked up by the thread pool yet are lost. They get reconciled by the next `sync-fills` poll, but the immediate live-fanout for those subscribers misses.
- **Redis pub/sub:** The fanout message sits in Redis until a worker acknowledges it. If a worker dies mid-handling, another worker picks up the unacknowledged message. **No fanout is dropped.**

### 3. Latency

- **In-process:** ~5–10ms dispatch overhead (thread pool submission + DB write). Total trader-click → all-subscribers-submitted: ~200–600ms depending on broker round-trips.
- **Redis pub/sub:** ~15–25ms dispatch overhead (Redis publish + worker pickup). Total: ~210–625ms. Slightly slower but indistinguishable to humans.

### 4. Operational complexity

- **In-process:** One backend service. One database. That's it.
- **Redis pub/sub:** One backend service + one (or more) worker service + Redis. Three things to deploy, monitor, scale, and pay for.

### 5. Cost on Render (interim host)

- **In-process:** Free tier covers dev. Starter ($7/mo) for production (always-on, no spin-down).
- **Redis pub/sub:** Add Render Key Value (Redis) starting at ~$10/mo + a second service for workers ($7/mo). **~$20–25/mo on top of the backend.**

### 6. Debugging and observability

- **In-process:** Trace by reading the FastAPI logs. Single process, single source of truth.
- **Redis pub/sub:** Tracing a single trade now spans the trader API process + Redis queue + workers. You'd want a tool like Grafana / OpenTelemetry to follow a request across hops. More moving parts means more to monitor.

---

## When does Redis become the right choice?

The architectural change is worth it when at least one of these is true:

1. **You're approaching ~100+ active subscribers per trader.** Single-process ThreadPool of 32 starts queuing; latency for the 33rd subscriber onward grows.
2. **You need >1 backend pod for any reason** — geographic distribution, HA across availability zones, load-balanced clusters. The moment you have two pods, the in-memory SSE bus breaks (a subscriber connected to pod A won't see events published from pod B). Redis fixes this on day one.
3. **You can't tolerate dropped fanouts on a crash.** Real money is at stake. Even if `sync-fills` catches up minutes later, missing a real-time fill notification might be unacceptable for some flows.
4. **The platform expands beyond one trader.** Multi-trader changes the fan-out math significantly — Redis becomes a natural way to isolate one trader's broadcasts from another's.

If none of those is true today, the in-process design is doing more with less.

---

## Migration cost (if/when we go ahead)

Concrete work to flip this:

| Change | Effort |
|---|---|
| Add `redis` Python client + worker package (e.g. `arq`, `dramatiq`, or `rq`) to `requirements.txt` | ~30 min |
| Refactor `services/events.py` from in-memory dict to Redis pub/sub (same `publish` / `subscribe` interface — only the body changes) | ~3 hours |
| Refactor `services/copy_engine.py` fanout: trader API publishes a `trade.fanout` message; new worker process consumes and calls `_place_one_subscriber`. The per-subscriber logic stays identical | ~half day |
| Add a separate worker service to `render.yaml` (one process running the worker library's run loop) | ~30 min |
| Add Redis Key-Value add-on on Render, set `REDIS_URL` env var on both services | ~15 min |
| Update `doc/DEPLOY.md` with the new architecture and env vars | ~30 min |
| Smoke test multi-pod scenarios | ~half day |

**Total: ~1.5 days of focused work, ~$20–25/mo additional infra.**

No frontend changes are needed. The user-facing behaviour stays identical (assuming the worker process is healthy and reasonably responsive).

The migration is reversible — the in-process design can be re-enabled by reverting two services files. So it's not a one-way door.

---

## Recommendation framework (not a recommendation)

| Situation | Suggested choice |
|---|---|
| < 50 subscribers, single trader, one Render pod, comfortable with the current scale | **Stay with in-process.** The Redis architecture adds complexity and cost for benefits you wouldn't use. |
| Planning to scale to 100+ subscribers or multi-pod within 3 months | **Migrate to Redis pub/sub now.** Doing it pre-emptively avoids a fire drill during growth. |
| Compliance / regulatory pressure for guaranteed-delivery audit trails | **Migrate to Redis pub/sub.** Acknowledgement / replay semantics matter here. |
| Operating budget extremely tight, willing to revisit at the scale milestone | **Stay with in-process, set a tripwire** (e.g. "when subscriber count crosses 75, migrate"). |
| Multi-trader product expansion is on the roadmap | **Migrate to Redis pub/sub before that work starts.** Worker model fits multi-trader naturally. |

---

## Questions for the client

If we're going to make this call cleanly, these are the inputs:

1. How many subscribers does the trader expect to onboard in the next 6 months? Year 1?
2. Is multi-trader (a second trader hosted on the same platform) a near-term goal?
3. What's the budget posture — is +$25/mo infra significant or routine?
4. What's the operational risk tolerance for a mid-fanout crash dropping ~5–20 subscriber notifications (until the next reconciliation poll catches up)?
5. Does the team have experience operating Redis + worker queues in production, or would this be a new operational skill to build?

Honest answers to those drive the decision much more than the architecture itself.
