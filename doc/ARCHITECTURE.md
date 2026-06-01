# Option Haven — Architecture & Design

> **What this is:** the enhanced copy-trading app ("App 2"), a separately
> deployed sibling of the original copy-trading platform ("App 1"). It mirrors
> one trader's orders to many subscribers, using a **queue-based parallel
> fanout** built for low latency and observability.
>
> **Status:** queue architecture + demo tooling are built. Broker, risk, and
> admin features are being consolidated in from other branches (see
> [§9 Current status](#9-current-status--whats-pending)).

---

## 1. The two apps

| | App 1 — copy-trading (production) | App 2 — Option Haven (this repo) |
|---|---|---|
| Repo | `Commoditycrm/copy-trading-app` | `Commoditycrm/options-trading` |
| Role | Original, live | Enhanced fork, deployed separately |
| Fanout | Redis Streams + worker pool | **Postgres queue + async worker pool** |
| Hosting | AWS Lightsail (Docker Compose, GitHub Actions auto-deploy on push to `main`) | AWS Lightsail (Docker Compose) — same model |

Both share the same foundation: **FastAPI + SQLAlchemy + Postgres + Alembic**
backend, **Next.js** frontend with **SSE** live updates, pluggable **broker
adapters** with Fernet-encrypted credentials, and a **retry scheduler** for
transient broker failures. The difference is entirely in **how the fanout is
architected**.

---

## 2. Requirements

### Functional
1. **Single trader (for now).** One trader whose orders everyone copies.
2. **Trader broker connections** (detection only — we read, never place):
   - **Webull** via **SnapTrade** *or* **direct**
   - **Alpaca** for testing (clean paper API)
3. **Subscriber broker connections** (placement — we submit orders):
   - **Alpaca**, **Webull via SnapTrade**, **IBKR**
4. **Broker-agnostic matching.** A subscriber on *any* broker can copy the
   trader regardless of which broker the trader uses. (No same-broker rule —
   it conflicts with IBKR being subscriber-only.)
5. **Per-subscriber retry settings** — if a subscriber's broker fails with a
   transient error, retry on a subscriber-chosen interval (separate for opens
   vs closes).
6. **Per-subscriber risk controls:**
   - **Daily loss limit** (per-trade / per-day) — auto-pause copying when hit
   - **Max drawdown %** — auto-pause when equity falls a set % from baseline
7. **Admin page** (for the operator):
   - View fanout **performance** (timings, per-subscriber timeline)
   - **Add fake subscribers** (load testing / demos)
   - **Add / remove** subscribers
8. **Faster than the previous versions** — minimal trader-side latency even
   with 100+ subscribers.

### Non-functional
- **Trader is never blocked** by subscriber count or a slow/failing broker.
- **Durable & observable** — every copy is an auditable record with timings.
- **No hard Redis dependency** for the core fanout.
- Deployable as its own Lightsail app, side by side with App 1.

---

## 3. Broker model: detection vs placement

The single most important concept:

```
TRADER side = DETECTION (read only)          SUBSCRIBER side = PLACEMENT (write)
─────────────────────────────────           ──────────────────────────────────
Trader trades in their own broker app        We submit mirror orders INTO each
(Webull / Alpaca). We only OBSERVE it.       subscriber's account.
We never place orders for the trader.        We need trade permission here.
```

- **Trader connection** only needs **read** access → this is why
  Webull-via-SnapTrade (historically read-only) was always fine for the trader.
- **Subscriber connection** needs **write** (place orders). As of **Dec 2025**,
  SnapTrade enabled **Webull trading**, so Webull-via-SnapTrade now works for
  subscribers too. (Confirm `connection_type=trade` is granted, not `read`.)

| Broker path | Trader (detect) | Subscriber (place) | Detection latency |
|---|---|---|---|
| Alpaca direct | ✅ | ✅ | < 1 s (WebSocket) |
| Webull direct | ✅ | ✅ | ~2–4 s (poll) |
| Webull via SnapTrade | ✅ | ✅ (since Dec 2025) | ~5 s (paid polling tier; ~10–60 s default) |
| IBKR | — (subscriber-only) | ✅ | — |

---

## 4. The fanout flow (100 subscribers, 1 trader)

```
STEP 1 — Trader trades on their own broker
   Trader buys 100 AAPL in Webull. (They do this; we don't.)

STEP 2 — DETECTION  (unified listeners, read-only)
   Listener observes the order.  Latency = broker-imposed:
     Alpaca <1s · Webull direct ~2–4s · Webull/SnapTrade ~5s (paid tier)
   ← slowest part, but outside our hot path.

STEP 3 — HOT PATH  (queue_fanout)                         ⏱ ~8 ms
   a. Read 100 followers + settings from IN-MEMORY cache   (~0 ms, no DB)
   b. Batch-INSERT 100 rows into pending_copies (Postgres) (~5–8 ms)
   c. Commit & return.  ← detection handler is now free.
   No eligibility checks, no broker calls here.

STEP 4 — WORKERS DRAIN THE QUEUE (in parallel)
   N async workers, each:
     • claim 1 row  (SELECT … FOR UPDATE SKIP LOCKED)    ~1 ms
     • read settings from memory cache                    ~0 ms
     • run gates (copy on? daily-loss? drawdown? qty>0?)  ~1–10 ms
     • PLACE mirror order at subscriber's broker          ~200–400 ms  ← dominant
     • write timestamps (picked_up / submitted)
   All workers run at once → wall-clock ≈ ONE broker call.

STEP 5 — OBSERVABILITY
   /admin/demo reads pending_copies and renders hot-path time,
   per-subscriber timeline bars, and status breakdown.
```

**Net for 100 subscribers:** order *queued* in **~8 ms**; all 100 broker
submissions finish in **~300–600 ms** (1–2 parallel waves), vs the old serial
**~2,300 ms+** that blocked the trader the whole time.

---

## 5. Mechanism comparison — previous vs now

| Step | Previous (App 1) | Now (App 2) | Why |
|---|---|---|---|
| Detect order | Alpaca WS + separate REST poller | **Unified `listeners`** (Alpaca/Webull/SnapTrade) | One service for every broker |
| Find subscribers | **DB query** each fanout | **In-memory cache** | DB work was the ~2,300 ms cost; RAM is ~0 ms |
| Enqueue work | **Redis Streams** `XADD` (or ThreadPool) | **Batch INSERT** into `pending_copies` | No Redis; 1 round-trip; rows = audit store |
| Distribute to workers | Redis **consumer group** (`XREADGROUP`/`XACK`) | **`SELECT … FOR UPDATE SKIP LOCKED`** | Same once-per-row guarantee, on Postgres we already run |
| Per-sub eligibility | DB reads in each task | Read from **memory cache** | Avoids 100× DB round-trips |
| Place at broker | Sequential within worker | **100 workers in parallel** | Total ≈ one broker call |
| Retry | `retry_scheduler` (DB poll) | **Same** (inherited) | Already works, broker-agnostic |

### Why NOT Redis pub/sub
Wrong tool for a work queue: **no persistence** (a message published while a
worker is down is lost forever — unacceptable for orders), **no replay/ack**,
and pub/sub **broadcasts** rather than distributing one message to one worker.
We'd have to rebuild durability on top of it.

### Why NOT Redis Streams (yet)
Redis Streams *is* a proper durable queue (App 1 uses it). We didn't use it here
because:
1. **One less dependency** — no second datastore to run/secure/pay for/monitor.
2. **`SKIP LOCKED` gives the same semantics** (competing consumers, once-per-row)
   on the Postgres we already have — plenty fast for 1 trader + ~100 subs.
3. **The queue rows are our observability store** — `pending_copies` carries the
   timing data the dashboard needs. With Redis we'd still persist results to
   Postgres anyway, running both.
4. **Durability + transactional consistency for free** — the insert commits with
   the rest of our state and survives restarts.
5. **Its throughput edge isn't needed yet** — that's the scaling threshold, not
   the single-trader case.

> **One-liner:** we swapped *DB-query → Redis Streams → workers* for
> *memory-cache → Postgres-queue (SKIP LOCKED) → workers*, trading Redis's
> distributed-scale strengths for **simplicity, zero extra infra, and built-in
> observability** — the right call at this scale. Redis Streams is the
> documented upgrade path (see §8).

---

## 5b. Measured results (local Docker, honest numbers)

Run on Docker Desktop (WSL2), **1 trader, 100 subscribers, mock broker**
(200–400 ms simulated latency + ~3% random failures), worker pool of 100,
Postgres `max_connections=250`, SQLAlchemy pool sized to the worker count.

| Metric | Measured | Note |
|---|---|---|
| **Hot path** (queue all 100 & return) | **~15–20 ms** | The headline win — trader is freed here |
| Outcome | **96–98 submitted / 2–4 failed** | failures = the mock broker's ~3% rejections → **per-subscriber fault isolation works** |
| Full drain (all 100 placed) | **~2.1–2.5 s** | NOT the "≈ one broker call" we projected — see below |

**The real win is confirmed:** the trader's hot path returns in **~15 ms**
regardless of subscriber count, vs the serial path that **blocks the trader
for ~2,300 ms**. That ~150× decoupling is the architecture's point and it
holds.

**What the run disproved:** total *completion* time did NOT collapse to one
broker call. Removing two hard ceilings (SQLAlchemy pool 15→140; default
thread-executor ~32→100) did **not** drop the drain below ~2 s, because the
next ceiling is fundamental to this implementation:

- **Python GIL + heavy ORM per row.** Each worker does several SQLAlchemy
  ORM queries + commits per copy. The broker `sleep()` releases the GIL (so
  those overlap), but the ORM/Python work between them serialises on the GIL
  — 100 threads can't run their Python portions at once.
- **100-way claim contention.** One-row-at-a-time `SELECT … FOR UPDATE SKIP
  LOCKED` + commit, ×100 concurrent, contends on the table/WAL.

So at 100 threads the design **decouples the trader brilliantly but does not
linearly parallelise completion**. Getting true ≈one-wave completion needs
process-based workers (escape the GIL), batched claims, and/or core-SQL
(not ORM) in the hot loop — see §8.

---

## 6. Components (App 2)

| Component | File | Responsibility |
|---|---|---|
| `pending_copies` table | `alembic/versions/d9a1b2c3e4f5_*` | Queue + per-row timing (`queued_at → picked_up_at → submitted_at`, `queue_to_broker_ms`, status) |
| `PendingCopy` model | `app/models/pending_copy.py` | ORM for the queue rows |
| Memory cache | `app/services/memory_cache.py` | In-process `{trader → [subscribers+settings]}`, loaded at startup, invalidated on changes |
| Queue fanout | `app/services/copy_engine.py :: queue_fanout` | Hot path: cache read → batch insert → return |
| Worker pool | `app/services/subscriber_worker.py` | N asyncio workers, `SKIP LOCKED` claim → gates → broker → record |
| Startup wiring + stats API | `app/main.py` | Loads cache, starts workers, exposes `/api/admin/demo/stats` |
| Demo dashboard | `frontend/app/(app)/admin/demo/page.tsx` | Live hot-path vs serial, timeline bars, status breakdown |
| Mock broker | `app/brokers/mock.py` | Simulated 200–400 ms latency + ~3% failures (no real keys) |
| Seed script | `backend/scripts/seed_demo.py` | 1 trader + N subscribers on mock broker, optional fire-order |
| Deploy stack | `docker-compose.demo.yml`, `deploy/`, `doc/DEPLOY_LIGHTSAIL_DEMO.md` | Lightsail single-box (Nginx + backend + frontend + Postgres) |

---

## 7. Advantages & disadvantages

### Advantages
- **Trader never blocked** — ~8 ms hot path regardless of subscriber count.
- **True parallelism** — total ≈ one broker call, not N.
- **Fault isolation** — one slow/failing subscriber can't delay the trader or
  the other 99.
- **No Redis** — queue lives in Postgres; one fewer moving part.
- **Durable & observable** — every copy is an auditable, timestamped row.

### Disadvantages / current limits
- **In-memory cache is single-process** — must be invalidated on every settings
  change; multiple backend instances would drift out of sync.
- **Postgres connection pressure** — each active worker holds a connection;
  default `max_connections=100`, so workers are capped at 50 (100 subs drain in
  2 waves) unless raised.
- **Workers run inside the API process** — share CPU with HTTP/SSE.
- **Polling, not push** — idle workers poll every 25 ms.
- **No per-broker rate limiting** — 100 simultaneous orders to one broker could
  hit rate limits.

---

## 8. When to improve (scaling thresholds)

| Trigger | What hurts | Fix |
|---|---|---|
| ~100–200 subs, 1 trader | Nothing — sweet spot | — (current target) |
| Run full 100 workers | SQLAlchemy app pool (15) **and** Postgres `max_connections` (100) | ✅ done: pool sized to worker count + Postgres `max_connections=250` |
| Want **completion** ≈ one broker call | **GIL + per-row ORM + claim contention** caps thread parallelism (~2 s drain at 100, measured §5b) | Process-based workers (escape GIL); batch-claim N rows per worker; core-SQL not ORM in the hot loop |
| ~500+ subscribers | 1-wave parallelism breaks; polling overhead | Dedicated worker service; polling → **Postgres LISTEN/NOTIFY** (or Redis) |
| Multiple backend instances (HA) | In-memory caches drift | **Shared cache** (Redis) or invalidation pub/sub |
| Multiple active traders at once | Queue contention | Partition workers; per-broker throttling |
| Very high throughput / strict durability | Postgres-as-queue strains | Adopt **Redis Streams** (App 1's model) |
| 100 orders to one broker | Broker rate limits / 429s | Per-broker rate limiter + existing retry scheduler |
| Faster Webull detection | ~5 s SnapTrade polling | **SnapTrade webhook** (push) — listener already written on `gaurav-snaptrade` |

---

## 9. Current status & what's pending

### Built (in `anitha-queue-demo`)
- Queue architecture: `pending_copies`, memory cache, `queue_fanout`, worker pool, startup wiring, stats API.
- Demo dashboard (`/admin/demo`).
- Mock broker + seed script.
- Lightsail deploy stack + guide.

### Pending — consolidation from other branches
Much of the remaining functionality already exists on `copy-trading-app`
branches and needs porting into this repo on top of the queue architecture:

| Area | Source branch | Work |
|---|---|---|
| **Webull + SnapTrade brokers** | `gaurav-snaptrade` | Port `webull.py`, `snaptrade.py`, connect flows (Webull MFA, SnapTrade portal start/finish), config keys, deps; add enum values via fresh migration on this repo's head |
| **Unified detection** | `gaurav-snaptrade` | Adopt the `listeners` service, feed detected orders into `queue_fanout` |
| **Risk controls** | `anitha-loss-limit-and-drawdown` | Port `daily_loss_limit_pct`, `per_trade_loss_limit_pct`, `max_drawdown_pct`, `max_drawdown_equity_baseline`; **wire them into the worker's eligibility gates** (worker currently checks only the absolute daily limit) |
| **Admin panel** | `anitha-admin` | Port ADMIN role + migration, `api/admin.py`, users page (add/remove), load-test page (add fake subs) |
| **Run end-to-end** | — | Local Docker demo with seeded subscribers |

### Future improvements
- SnapTrade **webhook** for near-instant Webull detection (code exists, needs a public URL + wiring).
- **Options placement** via SnapTrade/Webull (adapters are stocks-only for placement; option *detection* works).
- **Per-broker rate limiting**.
- **Auto-deploy CI** for App 2 mirroring App 1's GitHub Actions workflow (separate Lightsail instance).
- The scaling upgrades in §8 as volume grows.

---

## 10. Lag breakdown — inside vs outside our platform

Critical for understanding what we can and cannot optimize. All timings are
for a **1 trader + 100 subscribers** scenario on the options trading platform.

```
TRADER places an options order in their own broker app
──────────────────────────────────────────────────────────────────────────────
STAGE                        WHERE?          LATENCY        CAN WE CONTROL?
──────────────────────────────────────────────────────────────────────────────
Trader places order in their OUTSIDE         n/a            No — trader's
own Webull / Alpaca app      (their broker)                 own action

Broker accepts + routes      OUTSIDE         ~ms            No — broker
the order internally         (broker-side)                  internal

Detection (we observe        OUTSIDE         Alpaca WS < 1s No — imposed by
trader's order)              (broker-imposed) Webull direct 2–4s  the broker's
                                             SnapTrade poll 5s   API capability
                                             (paid 5s tier)

──────────────────────────────────────────────────────────────────────────────
←── ABOVE IS OUTSIDE OUR PLATFORM ── BELOW IS INSIDE OUR PLATFORM ──────────
──────────────────────────────────────────────────────────────────────────────

Read 100 subscribers from    INSIDE          ~0 ms          Yes — memory
in-memory cache              (our memory)                   cache design

Batch INSERT 100 rows        INSIDE          ~5–8 ms        Yes — single
into pending_copies          (our Postgres)                 batch insert

Commit & return              INSIDE          ~5 ms          Yes
(TRADER IS FREED HERE)

Worker: claim 1 row          INSIDE          ~1 ms/worker   Yes — SKIP LOCKED
(SELECT FOR UPDATE)          (our Postgres)

Worker: risk gates from      INSIDE          ~5–10 ms       Yes — memory-first
memory cache                 (our memory)                   design

Worker: place mirror order   OUTSIDE         ~200–400 ms    No — broker
at subscriber's IBKR         (IBKR broker)   (options)      network latency

All 100 placed (GIL-bounded) INSIDE          ~2 s           Partially — see §5b
                             (our process)                  for next optimizations

──────────────────────────────────────────────────────────────────────────────
AFTER OUR PLATFORM
──────────────────────────────────────────────────────────────────────────────

IBKR routes + matches        OUTSIDE         broker/venue   No
option order at exchange     (exchange)      dependent
```

### End-to-end summary (SnapTrade trader, 100 IBKR subscribers)

| Component | Time | Where |
|---|---|---|
| SnapTrade detects Webull order | ~5 s | **Outside** |
| Our hot path (detect → queue → return) | ~15–20 ms | **Inside ✅** |
| Worker gates + IBKR placement (all 100) | ~2 s | Inside + Outside |
| **Total user-perceptible** | **~7 s** | Dominated by external detection |
| **Trader blocked for** | **~15 ms** | Inside ✅ |

### What this means for optimization priorities

1. **Detection latency (~5 s)** — dominated by SnapTrade's polling cadence.
   Upgrade path: enable the SnapTrade **webhook** (code already written) for
   near-instant push detection. No code change needed on our side — just
   configure a public webhook URL in the SnapTrade dashboard.

2. **Hot path (~15–20 ms)** — already excellent. The trader is freed in ~15 ms
   regardless of subscriber count. No optimization needed.

3. **Drain (~2 s for 100 subs)** — GIL-bounded at current architecture.
   Upgrade path: process-based workers (escape GIL) + batched row claims.
   Not needed until subscriber count grows beyond ~200.

4. **IBKR options placement (~200–400 ms per order)** — broker-side, outside our
   control. IBKR is the fastest serious options broker available.

### Broker-specific detection latency (outside our platform)

| Trader broker | Detection method | Latency |
|---|---|---|
| Alpaca | WebSocket (push) | < 1 s |
| Alpaca | REST poller (fallback) | 1–2 s |
| Webull direct | 2 s poll | 2–4 s |
| Webull via SnapTrade | 5 s poll (paid tier) | ~5 s |
| Webull via SnapTrade + webhook | Push event | Near-instant |

### Subscriber placement latency — by broker + instrument

| Subscriber broker | Stock options placement | Notes |
|---|---|---|
| **IBKR** | ✅ ~200–400 ms | Primary subscriber broker; full option support including OCA bracket orders |
| Alpaca | ✅ ~100–200 ms | Paper trading only (current); options placement supported |
| Webull direct | ❌ Not yet | Options placement not built; stocks only currently |
| Webull via SnapTrade | ❌ Not yet | Options placement needs SnapTrade options discovery flow |
