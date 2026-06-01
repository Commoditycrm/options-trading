# Queue-Demo Deployment — AWS Lightsail (single box, Docker Compose)

Deploys the `anitha-trade-features` branch (App 2 — Option Haven) to its **own
Lightsail instance**, separate from App 1 (`copy-trading-app`, the serial-fanout
app on its own instance). Nothing here touches App 1.

```
            ┌────────────────────── Lightsail instance ──────────────────────┐
 Internet ─▶│ nginx :80/:443 ──/api/*─▶ backend (FastAPI + 100 async workers) │
            │                 └──/*────▶ frontend (Next.js)                    │
            │                              postgres (demo DB)                  │
            └─────────────────────────────────────────────────────────────────┘
```

Files used: `docker-compose.demo.yml`, `backend/Dockerfile`, `frontend/Dockerfile`,
`deploy/nginx.conf`, `deploy/.env.demo.example`.

---

## 1. Create the Lightsail instance

- Lightsail → Create instance → **Linux/Unix → OS Only → Ubuntu 24.04**
- Plan: **2 GB RAM / 2 vCPU ($12/mo)** recommended (100 workers + Postgres + Next.js).
  The 1 GB plan works for `QUEUE_DEMO_WORKER_COUNT=50`.
- Create a **static IP** and attach it (Networking tab) so the IP survives reboots.
- Open firewall ports (Networking → IPv4 firewall): **80** and **443** (22 is open by default).

## 2. Install Docker

SSH in (browser SSH or your key), then:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo usermod -aG docker $USER && newgrp docker
docker --version && docker compose version
```

## 3. Clone the demo branch

```bash
git clone https://github.com/Commoditycrm/options-trading.git
cd options-trading
git checkout anitha-trade-features
```

## 4. Configure secrets

```bash
cp deploy/.env.demo.example deploy/.env.demo
# Generate the two keys:
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(48))"
python3 -c "from cryptography.fernet import Fernet; print('CREDENTIAL_ENCRYPTION_KEY=' + Fernet.generate_key().decode())"
# (if cryptography isn't installed: pip3 install cryptography  — or generate on your laptop)
nano deploy/.env.demo   # paste keys, set POSTGRES_PASSWORD, set PUBLIC_URL=http://<STATIC_IP>
```

`deploy/.env.demo` is gitignored — it never gets committed.

## 5. Bring the stack up

```bash
docker compose -f docker-compose.demo.yml --env-file deploy/.env.demo up -d --build
```

- Migrations (`alembic upgrade head`, incl. the new `pending_copies` table) run
  automatically in the backend container's start command.
- Watch it come up:

```bash
docker compose -f docker-compose.demo.yml logs -f backend
# look for: "memory_cache: loaded N subscribers" and "subscriber_worker: started 50 worker(s)"
```

Then visit `http://<STATIC_IP>` — the demo dashboard is at
`http://<STATIC_IP>/admin/demo`.

## 6. (Optional) HTTPS with a domain

SSE works over plain HTTP for a quick demo, but browsers prefer HTTPS and some
networks block mixed content. If you have a domain:

1. Point an A record at the static IP.
2. Stop nginx briefly and run certbot in standalone mode (or use the webroot):
   ```bash
   sudo apt-get install -y certbot
   docker compose -f docker-compose.demo.yml stop nginx
   sudo certbot certonly --standalone -d your-demo-domain.com
   mkdir -p deploy/certs
   sudo cp /etc/letsencrypt/live/your-demo-domain.com/fullchain.pem deploy/certs/
   sudo cp /etc/letsencrypt/live/your-demo-domain.com/privkey.pem  deploy/certs/
   sudo chown $USER deploy/certs/*
   ```
3. Uncomment the `443` server block (and the HTTP→HTTPS redirect) in
   `deploy/nginx.conf`, set `server_name`, and update `PUBLIC_URL=https://...`
   in `deploy/.env.demo`.
4. `docker compose -f docker-compose.demo.yml up -d` to restart with TLS.

---

## Worker count vs. Postgres connections (important)

Each of the 100 workers opens a DB connection while it holds a claimed row +
submits the child order. Postgres defaults to `max_connections=100`, which the
backend's own pool + 100 workers would exceed — so `docker-compose.demo.yml`
**already** raises it to `max_connections=250` (see the `postgres` service
`command:`). No manual edit needed. Pick a worker count in `.env.demo`:

- **Default:** `QUEUE_DEMO_WORKER_COUNT=50` — safe everywhere, already a dramatic
  contrast vs. the ~2300 ms serial path.
- **Full 100:** set `QUEUE_DEMO_WORKER_COUNT=100` — fits under the 250 limit
  (backend pool tops out around 140 connections at 100 workers).

(The right long-term fix is a shared connection pool / smaller per-worker
session lifetime, but for the demo, tuning the worker count is enough.)

---

## Seed demo data (no real broker needed)

`scripts/seed_demo.py` creates a trader + N subscribers, each wired to a
**mock broker** (`app/brokers/mock.py`) that simulates 200-400ms order
latency with ~3% random failures — so the queue, worker pool, and dashboard
run end to end without any real broker credentials.

```bash
# Create 1 trader + 100 subscribers (all password: demo1234)
docker compose -f docker-compose.demo.yml exec backend python -m scripts.seed_demo --subscribers 100

# Same, AND immediately fire one trader order through the queue fanout
docker compose -f docker-compose.demo.yml exec backend python -m scripts.seed_demo --subscribers 100 --fire-order

# Wipe all demo data
docker compose -f docker-compose.demo.yml exec backend python -m scripts.seed_demo --reset
```

After `--fire-order`, open `/admin/demo` and watch the workers drain the
queue in real time. You can also log in as `demo-trader@signalboxx.test`
(password `demo1234`) and place orders from the Trade Panel to generate
fresh batches on demand.

> The mock broker is gated behind `BrokerName.MOCK` and only created by the
> seed script — production traders/subscribers are unaffected.

## Demo talking points (what the dashboard shows)

`/admin/demo` polls `/api/admin/demo/stats` every second and renders:

- **Queue hot path** (`detect → batch insert → return`) vs. **Serial equivalent**
  (`N × 23 ms`) — the headline contrast (~8 ms vs ~2300 ms for 100 subs).
- **Per-subscriber timeline bars**: gray = queue wait, green/red/amber = the
  parallel broker call, all starting at nearly the same instant (proving the
  fanout is parallel, not serial).
- **Status breakdown**: submitted / failed-with-reason counts.

To drive data: place a trader order on the demo app (Trade Panel) with several
subscribers following → rows appear on the dashboard within ~1s.

---

## Common commands

```bash
# Update to latest demo branch and redeploy
git pull && docker compose -f docker-compose.demo.yml --env-file deploy/.env.demo up -d --build

# Tail all logs
docker compose -f docker-compose.demo.yml logs -f

# psql into the demo DB
docker compose -f docker-compose.demo.yml exec postgres psql -U signalboxx signalboxx_demo

# Tear everything down (keeps the DB volume)
docker compose -f docker-compose.demo.yml down

# Tear down INCLUDING the DB volume (full reset)
docker compose -f docker-compose.demo.yml down -v
```
