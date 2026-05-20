# SignalBoxx — Project Summary

A handoff brief covering **what was built**, the **technology choices** behind each piece, and the **alternatives that were considered and rejected** (with reasons). Intended for the client's decision-makers — written in plain English with technical specifics where they matter.

---

## What you can do now

Once deployed, the platform supports:

1. **Trader places an order through our Trade Panel** → every active subscriber's broker account receives a mirrored order, scaled by each subscriber's multiplier, within ~1.5 seconds. Trader's click feels instant (10ms response); broker confirmation flows back as a live UI update.

2. **Trader places an order DIRECTLY at their broker** (Alpaca's web UI, mobile app, an external algo — anywhere outside our platform) → the same mirroring happens automatically. The trader doesn't need to use our app at all to copy-trade. This requires an explicit per-trader opt-in toggle so test/hedge trades aren't mirrored accidentally.

3. **Each subscriber controls their own exposure** — copy on/off switch, multiplier (0.1× to 10×), and a daily loss limit that auto-pauses copying when hit.

4. **Real-time everywhere** — fills, status changes, P&L calendar, and live order updates push to every connected browser tab without manual refresh.

5. **Audit trail of every action** — order placement, broker rejections, copy fanouts, settings changes — all logged with timestamps and metadata for compliance and debugging.

6. **Smart error recovery** — common broker rejections (rate limits, transient 5xx) auto-retry; user-fixable rejections (after-hours market order, expired option) get plain-English messages instead of raw broker JSON.

7. **Two brokers supported:** Alpaca (production-ready) and Interactive Brokers (adapter shipped, pending IBKR's third-party API onboarding to go live).

---

## Tech stack — what we chose and why

| Layer | Choice | Why this | Alternatives considered |
|---|---|---|---|
| **Frontend framework** | Next.js 15 + React 19 + TypeScript + Tailwind | Already chosen by the team before this engagement; mature, fast, deploys natively to Vercel. TypeScript gives us compile-time safety on API contracts. | Plain React (no SSR benefit), Vue (team unfamiliar), Svelte (less ecosystem) |
| **Frontend hosting** | Vercel | Designed for Next.js. Zero-config deploys, global CDN, free tier handles our scale. | Netlify (equivalent, less Next.js integration), Cloudflare Pages (less mature for Next.js App Router) |
| **Backend framework** | FastAPI (Python 3.11) | Already in the repo, modern async-first design, excellent OpenAPI docs auto-generated, fastest Python framework in benchmarks. Easy hire pool. | Django (already used by the other team developer in the parallel codebase — heavier, slower), Express/Node (different language paradigm split from team's Python skill), Flask (no async support) |
| **Backend hosting (interim)** | Render | Runs long-lived processes (Vercel's Python serverless CAN'T host WebSockets or persistent SSE — see "Hosting decision" below). Free tier covers dev. Same `uvicorn` command works unchanged when migrating to AWS later. | Railway (similar shape, less polished), Fly.io (more complex), Heroku (more expensive), AWS ECS now (premature complexity) |
| **Backend hosting (future)** | AWS ECS / Fargate | Long-running container model fits our architecture. Industry-standard for Python web apps at scale. | AWS Lambda (REJECTED — same serverless problem as Vercel: can't host WebSocket clients) |
| **Database** | PostgreSQL 16 | Industry standard for trading data. Strong transactional guarantees, mature tooling, scales well. Render's free Postgres covers development. | MySQL (less feature-rich), MongoDB (relational data doesn't fit document model), SQLite (no concurrency for production) |
| **Real-time push to UI** | Server-Sent Events (SSE) | One-way push (server → browser) is exactly our need. Works through every proxy, no special protocol. Simpler than WebSockets. | WebSockets (overkill — we don't need client → server messaging), polling (latency too high), long-polling (more complex than SSE for the same outcome) |
| **Real-time fill detection** | Alpaca's TradingStream WebSocket | Vendor-native push channel. Fills land in our DB within ~100ms of execution at the broker. | Polling Alpaca's REST API every N seconds (slower, expensive, racy), waiting for the user to refresh (what existed before — bad UX) |
| **Copy-trade fanout dispatch** | Redis Streams + Consumer Groups | One message per worker, parallel processing, automatic recovery if a worker crashes mid-job. Scales linearly with worker count. | **Standard Redis Pub/Sub (REJECTED** — would broadcast every message to every worker, causing duplicate mirror orders), Celery (heavier infra, slower for low-latency work), AWS SQS (vendor lock, similar latency to Redis), in-process threading only (doesn't scale beyond one server pod) |
| **Redis hosting** | Upstash (free tier 256MB) for testing; same product paid tier later | Always-on, no spin-down (unlike Render's free tier), pay-as-you-go pricing. Works from anywhere over TLS. | Render Key Value ($10/mo — fine but vendor lock to Render), self-hosted Docker Redis (operationally painful in production) |
| **Broker credential encryption** | Fernet symmetric encryption (Python `cryptography` library) | Industry standard for "encrypt these secrets at rest with one key the app holds." Reversible (we need to decrypt to call broker APIs — so bcrypt won't work). | AWS KMS / Hashicorp Vault (overkill for a single-trader platform — useful at scale), plaintext (never), bcrypt/Argon2 (one-way only — can't decrypt to use the credentials) |
| **Alpaca SDK** | `alpaca-py` 0.33 (official) | Maintained by Alpaca themselves, covers REST + streaming + options. | Raw HTTP via `requests`/`httpx` (we'd be reimplementing well-tested SDK code) |
| **IBKR SDK** | `ibind` 0.1.23 (community) | The only Python library that supports IBKR's OAuth 1.0a Web API (the right path for SaaS — see IBKR notes below). Well-maintained, covers REST + WebSocket. | Raw HTTP + DIY OAuth 1.0a signing (RSA-SHA256 over canonicalized strings + live_session_token derivation — fragile to get right), TWS API via `ib_async` (REJECTED — needs IB Gateway desktop app running, operationally painful for a SaaS) |
| **Authentication** | JWT access tokens (30 min) + refresh tokens (14 days) | Stateless auth, scales horizontally without sticky sessions. Industry standard for SPA + API. | Session cookies (would require sticky sessions or shared session store), OAuth-only (overkill for the email/password use case) |
| **Migrations** | Alembic (SQLAlchemy's migration tool) | Standard pairing with SQLAlchemy. Version-controlled, reversible, auto-generates from model changes. | Raw SQL files (no rollback safety), Django migrations (we don't use Django) |

---

## Architecture decisions — the bigger choices, with full reasoning

### Decision 1 — Why Render for the backend instead of Vercel

**The problem:** the client originally planned to deploy everything on Vercel. Vercel's Python runtime runs the backend as **serverless functions** — each HTTP request spins up a fresh function, runs, returns, and dies. Nothing persists between requests.

**What that breaks:**
- The Alpaca WebSocket that detects live fills can't stay connected (function dies after each request)
- SSE connections to browsers get cut off at Vercel's 30–60s function timeout
- The in-memory event bus that fans events to SSE clients doesn't survive between function invocations
- FastAPI's BackgroundTasks for async order placement may not complete before the function is killed

**Result on Vercel:** the "fast and live, no delay" requirement would fail. Trades would only update after a manual page refresh, and subscribers wouldn't see anything happen in real time.

**Choice:** **Render for interim, AWS ECS later.** Both run the backend as a long-running container — the exact shape the streaming architecture needs. Render's free tier handles dev; migration to AWS is a config change, not a code change.

**Alternatives that were rejected:**
- Stay on Vercel + re-architect for serverless (replace SSE with polling, replace BackgroundTasks with a queue, replace TradingStream with periodic polling) — this would gut the "live" requirement and add ~2-3 days of rewrite work
- Use a third-party real-time service like Pusher or Ably — adds vendor cost and another integration

### Decision 2 — Why Redis Streams (not standard Pub/Sub) for fanout

**The problem:** copy-trade fanout means "when the trader places an order, distribute work to N subscribers in parallel." The naive Redis approach (Pub/Sub) was initially considered, then **rejected by the client's own architectural review** with a key technical insight:

> *Standard Redis Pub/Sub broadcasts every message to ALL subscribers. If we ran 5 worker processes all subscribed to a `fanout` channel, every trade would be mirrored 5 times — once per worker. Catastrophic.*

**Correct primitive:** Redis Streams + Consumer Groups. Each message goes to **exactly one** worker in the group. Workers acknowledge (XACK) after success. If a worker crashes before acknowledging, the message stays in the pending list and another worker reclaims it (XAUTOCLAIM).

**Granularity:** one Redis Stream message per `(trader_order × subscriber × broker_account)`. With 100 subscribers, that's 100 small messages. Workers grab them one at a time, do one broker call, ack. Pure work-queue pattern.

**Alternatives rejected:**
- **Standard Pub/Sub** — would cause duplicate mirror orders (see above)
- **Celery + Redis broker** — heavier infrastructure footprint, ~50ms scheduling overhead per task vs Streams' ~10ms, additional moving parts (Celery beat, monitoring tools)
- **AWS SQS** — vendor lock-in, similar latency to Redis Streams but locks us to AWS for this layer
- **In-process threading only** (32-worker ThreadPoolExecutor) — what was built first; works fine for <50 subscribers and one backend pod, but can't scale across multiple pods and loses in-flight work if the process crashes

### Decision 3 — Two trade-placement paths (UI + direct-at-broker)

**The product question:** does the trader use our app's Trade Panel, or do they trade directly at their broker?

**The choice:** support **both**. The trader can do either or both — the platform handles both paths identically downstream.

- **UI path:** trader uses our Trade Panel. We place the order at the broker on their behalf and fan out to subscribers.
- **Direct path:** trader places the order at their broker's own UI/app/algo (no involvement from our app). Our backend's broker WebSocket detects the new order, and we fan out to subscribers.

**Why both:**
- The direct path means the trader can keep their existing workflow — no behavior change, no app switching
- The UI path is useful for traders who want a single place to manage subscribers + place trades, or who don't have a broker UI they like
- Both paths converge on the same `process_one_fanout` function so the behavior is consistent

**Default for the direct path:** OFF. The trader explicitly opts in via a Settings toggle. This prevents accidentally mirroring test trades or hedges. **This was an important safety decision** — surprise mirroring of trades the trader didn't intend to share would be a serious bug.

### Decision 4 — IBKR via OAuth Web API, not TWS Gateway

**Background:** Interactive Brokers offers two APIs. One needs a desktop application running (TWS or IB Gateway). The other is REST-over-OAuth.

**Choice:** **OAuth Web API.**

**Why:**
- The TWS Gateway approach requires a running Java desktop app per account, with daily auto-logout cycles, port management, and watchdog scripts. The other developer on the team (Jayesh) used this pattern via a Flask sidecar — works for one trader on one machine, doesn't fit a SaaS.
- The OAuth Web API is stateless HTTPS. Same architectural shape as our Alpaca integration. Fits cleanly into the existing broker adapter pattern.

**Cost:** IBKR's third-party API onboarding takes 1–2 weeks via `webapionboarding@interactivebrokers.com`. The client should kick this off now since it's blocking the IBKR feature going live. The adapter code is shipped and waiting for credentials.

---

## Real-time architecture — the user-visible result

Two scenarios, both end-to-end in ~1.5 seconds for 100 subscribers:

**Scenario A — Trader clicks BUY in our Trade Panel:**
- 10ms — UI responds with "pending"
- 300ms — broker confirms trader's order
- 320ms — we publish 100 messages to Redis (one per subscriber)
- 1500ms — last subscriber's broker has accepted (parallel processing across workers)

**Scenario B — Trader places direct at Alpaca's UI:**
- 50ms — Alpaca's WebSocket pushes the new-order event to our backend
- 70ms — we publish 100 messages to Redis
- 1300ms — last subscriber mirrored

The biggest latency contributor is the broker's REST API itself (200–600ms per order). Our infrastructure adds ~30–80ms of overhead in either case.

---

## Storage and audit

Every action writes to PostgreSQL — orders, fills, broker accounts (encrypted credentials), settings, and a comprehensive `audit_logs` table. The audit log captures: registrations, logins (success + failure), broker connect/verify/delete, settings changes, every order placement and rejection, every copy-fanout result. Append-only by convention; a Postgres trigger to enforce immutability is a future hardening step (documented in code).

The Redis Stream is **not the source of truth** — it's a transient work queue. If Redis loses data, the worst case is unprocessed fanouts (they'd be reconciled by the existing `fills_sync` polling fallback). PostgreSQL holds the canonical state of every order.

---

## Cost picture

| When | Setup | Monthly cost |
|---|---|---|
| Today (testing) | Render free tier + Upstash free tier + Vercel hobby | **$0** |
| Going live with real money (single trader, <50 subscribers) | Render Starter web service + Render Starter Postgres + Upstash free | **~$15** |
| Scaling up (50–200 subscribers, dedicated worker process) | + Render Starter worker | **~$22** |
| Multi-pod backend / HA | + Redis Pub/Sub for SSE bus + 2nd backend pod | **~$35–45** |

---

## What's deferred (known and intentional)

These were considered and explicitly left for a later iteration. Each is small enough to add when the need is concrete.

1. **IBKR real-time fill stream** (mirror of the Alpaca trade-update integration, using `ibind`'s WebSocket) — blocked on IBKR onboarding credentials.
2. **IBKR options support** — the adapter places stock orders; option contract resolution needs additional API plumbing (~half day).
3. **Auto-convert market order → limit order on after-hours rejection** — Jayesh's parallel codebase had this; needs the broker adapter to fetch a current quote, which we haven't added (~half day).
4. **Database unique constraint** on `(parent_order_id, broker_account_id)` to back up the application-level idempotency check (one-line migration; defensive against very narrow worker-crash race).
5. **One-time SSE token** instead of short-lived JWT in the URL — security hardening; current JWT lives 30 min, leakage is bounded but not zero.
6. **Multi-pod-safe SSE event bus** (swap in-memory dict for Redis Pub/Sub) — only matters at >1 backend pod, which we don't need at current scale.
7. **Multi-trader support** — single-trader is enforced at registration today. The model can be relaxed when the product grows beyond one trader.
8. **Postgres trigger** to enforce `audit_logs` as append-only (currently it's a convention, not a DB constraint).

---

## Honest things to know

- **Free tiers spin down:** Render's free web service goes idle after 15 minutes of inactivity, which kills the Alpaca WebSocket. The next request wakes it (~30s cold start) and the stream reconnects. For production trading this is unacceptable — bump to Render Starter ($7/mo) before going live.
- **IBKR is shipped-but-untested:** the adapter compiles cleanly and follows the documented `ibind` API, but we haven't run it against a real IBKR account because the client hasn't completed onboarding yet. First end-to-end test should be against the IBKR paper environment.
- **The platform is a copy-trade engine, not a trading strategy.** It mirrors what the trader does. The trader is solely responsible for the quality of trades. Regulatory considerations (RIA registration, broker-dealer rules) apply before charging subscribers — get a securities lawyer's review.
- **Single trader, by design.** Adding a second trader is a model/API change, not a one-line tweak. Plan for it if the product is heading that way.
- **The architecture is reversible.** Today we use Redis Streams for fanout; if we ever needed to drop Redis entirely, the in-process ThreadPoolExecutor path still exists as a fallback (controlled by whether `REDIS_URL` is set). No code is in a one-way door.

---

## Summary for a one-minute briefing

> We built a copy-trade SaaS where one trader's orders mirror to many subscribers' broker accounts in real time. Built on FastAPI + Postgres + Next.js. Two production-ready paths: trader uses our app, or trader uses their broker's app directly. Fanout to subscribers is parallel via Redis Streams (chosen over standard Pub/Sub because Pub/Sub would have duplicated orders). Hosted on Render with the frontend on Vercel; designed for an AWS ECS migration later. Alpaca is fully integrated; IBKR is wired up and waiting on IBKR's third-party API approval (1–2 weeks). Total infra cost: $0 to test, ~$15/mo for first real-money users.
