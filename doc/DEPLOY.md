# Deployment

**Topology:** Frontend on Vercel, backend on Render (interim) → AWS ECS/Fargate (later). The backend code is the same on Render and AWS — only the host changes.

## Why the backend isn't on Vercel

Vercel runs Python as serverless functions. The backend holds state that serverless can't host:

- One Alpaca (later IBKR) WebSocket per connected broker for live order/fill push
- Per-user SSE streams kept open until the browser tab closes
- FastAPI `BackgroundTasks` that submit orders to the broker asynchronously

All three want a long-running process. Serverless gives us a process that lives ~one request and dies. The "fast, live, no delay" requirement needs a long-running host.

Render (interim, free tier OK for dev) and AWS ECS/Fargate (later, production) both give us that. The same `uvicorn app.main:app` start command runs on both.

## Render setup

1. **From the Render dashboard:** New → Blueprint → connect this GitHub repo → Render auto-detects `render.yaml`.
2. **After the service is created, set the Fernet key by hand.** `render.yaml` marks it `sync: false` so it isn't auto-generated (Render's `generateValue` doesn't produce a valid Fernet key shape). Generate locally and paste into Render → Environment:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set `CREDENTIAL_ENCRYPTION_KEY` to that value. **Never rotate this in place** — every encrypted broker credential in the DB will become unreadable.

3. **Update `CORS_ORIGINS` and `FRONTEND_BASE_URL`** in `render.yaml` to match your actual Vercel URL (default assumes `copy-trading-app.vercel.app`).

4. **Free tier limitation:** the service spins down after 15 minutes of inactivity. When it spins down, the Alpaca trade-update WebSocket dies and any open SSE connection drops. The next request wakes it (30+ second cold start) and the stream reconnects. **Bump to the Starter plan ($7/mo) before going live** — the always-on guarantee is what makes the streaming features work in production.

## Vercel frontend setup

1. **Existing `vercel.json` now points at the frontend only** — no more `@vercel/python` build, no `/api/*` rewrite to a local backend.
2. **Set two env vars in the Vercel project:**

   | Name | Value | Why |
   |---|---|---|
   | `BACKEND_URL` | `https://signalboxx-backend.onrender.com` | Used server-side by `next.config.js`'s rewrite, so the browser's `/api/*` calls proxy through Next.js to Render. Not exposed to the browser. |
   | `NEXT_PUBLIC_API_BASE_URL` | `https://signalboxx-backend.onrender.com` | Exposed to the browser. Used **only by the SSE client (`lib/sse.ts`)** so the EventSource hits Render directly, bypassing Vercel's proxy (which would time out long connections at ~30–60s). |

3. Redeploy the frontend. Test by opening the Trades page and confirming the SSE event stream stays connected past 60s (network tab → `/api/events` → "EventStream" should stay open).

## Local dev

Unchanged. `next.config.js` rewrites default to `http://localhost:8000`. Run the backend on port 8000, frontend on port 3000, no env vars needed. SSE works through the Next.js dev server's rewrite without timeout issues.

## Migration to AWS later

When the team is ready for AWS:

- **ECS / Fargate / App Runner / EC2** — all long-running, all run `uvicorn app.main:app` unchanged. Pick whichever fits your infra.
- **Avoid AWS Lambda** — same serverless problem as Vercel. Won't host the WebSocket / SSE / background-task pieces.
- Postgres: migrate to RDS, update `DATABASE_URL`.
- DNS: point the Render URL to the new AWS URL. Update `BACKEND_URL` and `NEXT_PUBLIC_API_BASE_URL` on Vercel.

## Required env vars (reference)

| Variable | Where | Notes |
|---|---|---|
| `DATABASE_URL` | backend | Postgres connection string. Render fills it automatically from `signalboxx-db`. |
| `JWT_SECRET` | backend | 256-bit base64. Render generates it. **Never rotate in place** — invalidates all live sessions. |
| `JWT_ALGORITHM` | backend | `HS256`. Don't change unless you've coordinated with the frontend. |
| `JWT_ACCESS_TOKEN_MINUTES` | backend | `30` default. |
| `JWT_REFRESH_TOKEN_DAYS` | backend | `14` default. |
| `CREDENTIAL_ENCRYPTION_KEY` | backend | Fernet key. Set by hand (see above). **Never rotate** — invalidates every stored broker credential. |
| `CORS_ORIGINS` | backend | Comma-separated. Must include the Vercel frontend URL. |
| `FRONTEND_BASE_URL` | backend | Used in password-reset emails. |
| `BACKEND_URL` | Vercel (server) | Render URL. Used by Next.js rewrites. |
| `NEXT_PUBLIC_API_BASE_URL` | Vercel (browser) | Render URL. Used by EventSource. Required for live updates in production. |
| `IBKR_CONSUMER_KEY` | backend (optional) | App-level. Issued by IBKR on third-party API onboarding approval. Leave blank to disable the IBKR broker option. |
| `IBKR_DH_PARAM_PEM` | backend (optional) | Multiline PEM contents (not a file path). Generate per IBKR's onboarding docs; upload the public half to your IBKR developer console. |
| `IBKR_PRIVATE_ENCRYPTION_PEM` | backend (optional) | Multiline PEM contents. Same pattern as DH param. |
| `IBKR_PRIVATE_SIGNATURE_PEM` | backend (optional) | Multiline PEM contents. Same pattern. |
| `REDIS_URL` | backend + worker (optional) | Full `redis://` or `rediss://` URL. Drives the Redis-Streams copy-trade fanout. Leave blank to fall back to in-process ThreadPoolExecutor dispatch (single-pod only). See "Redis Streams setup" below. |
| `RUN_FANOUT_WORKER_IN_PROCESS` | backend (optional) | `true` (default) runs a fanout worker as an asyncio task inside the FastAPI process — convenient for local + single-Render-service deploys. Set `false` only when you've enabled the dedicated worker service in render.yaml so you don't run two consumer instances at once. |

### Redis Streams setup

The copy-trade fanout uses Redis Streams + Consumer Groups so multiple worker processes can share the work in parallel without duplicating mirror orders (which standard Redis Pub/Sub would do — it broadcasts every message to every subscriber). With Streams, each `(trader_order, subscriber, broker_account)` message goes to exactly one worker.

**Local dev:** the `docker-compose.yml` brings Redis up alongside Postgres. Set `REDIS_URL=redis://localhost:6379/0` in your `.env`. The FastAPI process also runs a fanout worker in-process by default (`RUN_FANOUT_WORKER_IN_PROCESS=true`) so you don't manage a separate worker process locally.

**Render / production:** the simplest path is **Upstash free tier** — sign up at https://upstash.com, create a Redis database (the free tier gives you 256MB storage, 10K commands/day, always-on, no spin-down), copy the connection URL, paste into Render → Environment → `REDIS_URL`. Render's own Key-Value service works too at ~$10/mo if you want everything in one dashboard.

**Scaling to a dedicated worker service** (when single-pod isn't enough):

1. In `render.yaml`, uncomment the `signalboxx-fanout-worker` block.
2. Set `RUN_FANOUT_WORKER_IN_PROCESS=false` on the backend so you don't have two consumers competing.
3. Deploy. The worker service runs `python worker.py` and consumes from the same stream/group as the in-process worker did. Scale the worker count up/down independently of the backend.

**Inspecting the stream** (debugging fanout):

```bash
# Show pending messages waiting on the consumer group
redis-cli XPENDING signalboxx:fanout fanout_workers

# Show stream metadata + active consumers
redis-cli XINFO STREAM signalboxx:fanout
redis-cli XINFO CONSUMERS signalboxx:fanout fanout_workers

# Read the last 10 messages (regardless of consumer group)
redis-cli XREVRANGE signalboxx:fanout + - COUNT 10
```

### IBKR onboarding (one-time, app-level)

The IBKR broker option will surface in the UI as soon as the adapter is deployed, but **connect attempts return 501 until all four `IBKR_*` env vars are set**. To enable:

1. Email `webapionboarding@interactivebrokers.com` to start the third-party API approval flow. Expect 1–2 weeks for Compliance review.
2. Once approved, generate the three signing PEMs locally per IBKR's docs and upload the public halves to your IBKR developer console.
3. IBKR returns the `consumer_key`. Set the four `IBKR_*` env vars in Render.
4. Each end-user (subscriber) then authorizes against IBKR via OAuth and pastes their access token + access token secret + account ID into the broker connect form.
