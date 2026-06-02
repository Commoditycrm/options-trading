# Option Haven — Handoff Document
> Paste this as the first message in a new session.

---

## What this project is
**Option Haven** — an options copy-trading platform (App 2). One trader's option
orders are detected and mirrored to many subscribers in parallel. Built on a
**queue-based fanout** architecture so the trader is freed in ~15 ms regardless
of subscriber count.

- **Repo:** `https://github.com/Commoditycrm/options-trading`
- **Working branch:** `anitha-trade-features` (pushed + synced with origin)
- **Parent branch:** `anitha-brokers` (pushed + synced with origin)
- **Architecture doc:** `doc/ARCHITECTURE.md` (lag breakdown in §10)
- **Deploy guide:** `doc/DEPLOY_LIGHTSAIL_DEMO.md`
- **This file:** `doc/HANDOFF.md`

## Repo location on disk
```
C:\Users\anith\Clade- Copy trading\options-trading\
```

## Git state right now
```
branch:  anitha-trade-features   (latest work, pushed + synced with origin)
parent:  anitha-brokers          (pushed + synced with origin)
remote:  origin → Commoditycrm/options-trading
```
**Both branches are pushed and in sync with origin** — the box can `git clone`
directly. To pull the latest before deploying:
```powershell
cd "C:\Users\anith\Clade- Copy trading\options-trading"
git fetch origin
git checkout anitha-trade-features && git pull
```

---

## Alembic migration chain (current head: `e2f3a4b5c601`)
```
a92fc3b551d4  add retry + notifications (inherited baseline)
d9a1b2c3e4f5  add pending_copies queue table
e1f2a3b4c5d6  add 'mock' broker enum value
b7e4c2a9f013  add 'webull' + 'snaptrade' broker enum values
c4d5e6f7a801  add pct risk limits (daily_loss_limit_pct, per_trade, max_drawdown)
d5e6f7a8b902  add 'admin' to user_role enum
e2f3a4b5c601  add trade features (excluded_symbols, mirror_only_filled,
               default_broker_account_id, take_profit_pct, stop_loss_pct)  ← HEAD
```

---

## Features built (complete list)

### Queue architecture (the core)
- `pending_copies` table — queue rows with timing timestamps
- In-memory subscriber cache (`memory_cache.py`) — zero-DB hot path
- `queue_fanout()` — batch INSERT ~8ms for 100 subs, trader freed immediately
- 100 async worker pool with dedicated `ThreadPoolExecutor` + sized SQLAlchemy pool
- `dispatch_detected_order()` — single entrypoint from all detection paths
- Admin demo dashboard at `/admin/demo`

### Brokers (trader-side detection + subscriber-side placement)
| Broker | Trader detect | Subscriber place | Notes |
|--------|--------------|-----------------|-------|
| Alpaca | ✅ WS + REST poller | ✅ paper trading | Working, tested |
| Webull direct | ✅ 2s poll | ❌ not built | Adapter exists; option placement deferred |
| Webull via SnapTrade | ✅ 5s poll + webhook | ❌ read-only for options | Detection works; SnapTrade Webull = trade-capable for stocks only in options context |
| **IBKR** | 🔲 future | ✅ **PRIMARY subscriber broker** | Full options + OCA brackets |

### Risk controls (all wired into the worker)
- `daily_loss_limit` (absolute $)
- `daily_loss_limit_pct` (% of equity)
- `per_trade_loss_limit_pct` (last closed trade % of equity)
- `max_drawdown_pct` (% from equity baseline)
- All auto-pause `copy_enabled` + invalidate cache + SSE notify on trip

### New trade features (latest commit `8c81657`)
| # | Feature | Status |
|---|---------|--------|
| #3 | `TraderSettings.mirror_only_filled` — only FILLED orders mirrored | ✅ |
| #4 | Auto TP/SL on options via IBKR OCA brackets (Replace mode) | ✅ backend; frontend pending |
| #5 | Lag breakdown table in ARCHITECTURE.md §10 | ✅ |
| #6 | `excluded_symbols TEXT[]` — per-subscriber stock skip list | ✅ |
| #1B | Trader connects both Alpaca + Webull (no eviction); `default_broker_account_id` | ✅ backend; frontend pending |

### Admin panel
- `UserRole.ADMIN` + `/api/admin/*` + `scripts/create_admin.py`
- `/admin` dashboard, `/admin/users`, `/admin/load-test`, `/admin/demo`

### Mock broker + seed
- `MockAdapter` — 200–400 ms simulated latency, ~3% failures
- `scripts/seed_demo.py --subscribers 100 --fire-order`

---

## AWS Lightsail deploy — next steps (instance is ready)

### Step 1 — SSH into the box, install Docker
```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker
```

### Step 2 — Clone the repo, checkout the branch
```bash
git clone https://github.com/Commoditycrm/options-trading.git
cd options-trading
git checkout anitha-trade-features
```

### Step 3 — Create secrets file
```bash
cp deploy/.env.demo.example deploy/.env.demo
nano deploy/.env.demo
```
Fill in:
- `POSTGRES_PASSWORD` — any strong password
- `JWT_SECRET` — `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
- `CREDENTIAL_ENCRYPTION_KEY` — `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `PUBLIC_URL=http://<your-lightsail-static-ip>`
- `QUEUE_DEMO_WORKER_COUNT=50` (safe default; raise to 100 after confirming DB connections)

### Step 4 — Bring up the stack (auto-runs migrations)
```bash
docker compose -f docker-compose.demo.yml --env-file deploy/.env.demo up -d --build
```
Watch migrations + startup:
```bash
docker compose -f docker-compose.demo.yml logs -f backend
# Look for: "subscriber_worker: started 50 worker(s)"
```

### Step 5 — Seed data + create admin
```bash
# 1 trader + 100 subscribers on mock broker; fire one order
docker compose -f docker-compose.demo.yml exec backend \
  python -m scripts.seed_demo --subscribers 100 --fire-order

# Create your admin login
docker compose -f docker-compose.demo.yml exec backend \
  python scripts/create_admin.py --email you@example.com --create
```

### Step 6 — Open in browser
- `http://<ip>/login` → log in as `demo-trader@optionhaven.test` / `demo1234`
- `http://<ip>/admin` → log in as your admin email (from step 5)
- `http://<ip>/admin/demo` → queue dashboard (live timings)

### Lightsail firewall
Networking tab → IPv4 firewall: **keep only 80, 443, 22**. Remove any 5432/6379.

---

## IBKR onboarding (for real subscriber connections)
IBKR requires a third-party app approval before any API connection works.
Steps:
1. Email `webapionboarding@interactivebrokers.com` — request third-party API access
2. They send back 4 credentials: `IBKR_CONSUMER_KEY` + 3 PEM files
3. Set these as env vars on the Lightsail box:
   ```
   IBKR_CONSUMER_KEY=...
   IBKR_DH_PARAM_PEM=...
   IBKR_PRIVATE_ENCRYPTION_PEM=...
   IBKR_PRIVATE_SIGNATURE_PEM=...
   ```
4. Restart the backend. Subscribers can now connect IBKR at `/brokers`.

---

## What's NOT done yet (in priority order)

| Item | Effort | Notes |
|------|--------|-------|
| **Frontend: broker dropdown** (Trade Panel, pick Alpaca vs Webull) | S | Backend done; needs UI dropdown + default-broker setter |
| **Frontend: exclusion list** (tag-input on subscriber settings) | S | Backend done; needs UI |
| **Frontend: TP/SL inputs** (subscriber settings) | S | Backend done; needs UI |
| **Frontend: mirror-only-filled toggle** (trader settings) | S | Backend done; needs UI |
| **GitHub Actions auto-deploy** for App 2 | S | Same pattern as App 1's `deploy.yml`; point at a separate Lightsail instance |
| **SnapTrade options placement** | M | Needs SnapTrade option-discovery flow; currently stocks-only |
| **Webull direct option placement** | M | Unofficial SDK partial support; fragile |
| **Phase 3b live test** (real Webull/SnapTrade broker) | — | Needs sandbox credentials; code is import-clean |
| **Process-based workers** (escape Python GIL for true parallelism) | L | Current: ~2 s drain for 100 subs via threads; process workers would cut this |
| **RETRY_PENDING enum bug** (retry_scheduler logs errors) | XS | Already spawned as a task chip — ORDER status Enum needs `values_callable` |

---

## Key files to know
```
backend/
  app/
    models/
      settings.py        — SubscriberSettings + TraderSettings (all new fields)
      pending_copy.py    — Queue row model
    services/
      copy_engine.py     — queue_fanout() + dispatch_detected_order()
      memory_cache.py    — SubscriberCacheEntry (all fields)
      subscriber_worker.py — 100 workers, all risk gates, bracket logic
      listeners.py       — unified detection dispatcher (Alpaca/Webull/SnapTrade)
    brokers/
      ibkr.py            — place_bracket_order() OCA implementation
      base.py            — place_bracket_order() interface
    api/
      settings.py        — all settings endpoints inc. new ones
      brokers.py         — connect flows + SnapTrade webhook
  alembic/versions/      — 7 migrations, head = e2f3a4b5c601
  scripts/
    seed_demo.py         — 100-subscriber seeder
    create_admin.py      — promote/create admin user
docker-compose.demo.yml  — full stack (nginx+backend+frontend+postgres)
deploy/
  .env.demo.example      — secrets template
  nginx.conf             — nginx config
doc/
  ARCHITECTURE.md        — full architecture, measured results, lag table
  DEPLOY_LIGHTSAIL_DEMO.md — step-by-step AWS guide
  HANDOFF.md             — this file
```

---

## Important rules (don't break these)
- **Never touch `main` or Gaurav's branches** in `copy-trading-app`
- **`options-trading` is App 2** — all our work goes here
- **`anitha-trade-features` branches off `anitha-brokers`** — preserve that lineage
- The demo DB (`optionhaven_demo`) is isolated from App 1 — its own Postgres container + volume on App 2's own Lightsail instance
- `deploy/.env.demo` is gitignored — never commit secrets
