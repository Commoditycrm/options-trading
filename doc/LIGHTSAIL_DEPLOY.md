# Self-hosted Lightsail deployment

Single-instance deployment of the whole stack (frontend + backend + worker +
Postgres + Redis) behind Caddy with automatic TLS. All services run as Docker
containers from `docker-compose.prod.yml`.

> This is the **self-hosted** path. The Render + Vercel path in
> [DEPLOY.md](DEPLOY.md) is unchanged and still valid for managed hosting.

## Security model (why this is safe to expose)

- **Only Caddy publishes ports** (80/443). Postgres (5432) and Redis (6379)
  have **no port mappings** — they live on the private `internal` Docker
  network and are unreachable from the internet.
- **Redis requires a password** (`--requirepass`), closing the
  unauthenticated-Redis remote-code-execution hole.
- **Postgres uses a strong generated password** (not the dev `trading/trading`).
- Secrets live in a gitignored `.env` on the host — never in the image, never
  in git.
- Containers run as **non-root** users.

## 1. Provision the Lightsail instance

- **Blueprint:** "OS Only → Ubuntu 22.04 LTS".
- **Plan:** at least **2 GB RAM** (Next build + Postgres + Redis + two Python
  services). 1 GB will OOM during `next build`.
- **Networking → IPv4 firewall — open ONLY:**
  | Port | Source | Purpose |
  |---|---|---|
  | 22 (SSH) | your IP only (tighten from "Any") | admin |
  | 80 (HTTP) | Any | ACME challenge + redirect to HTTPS |
  | 443 (HTTPS) | Any | the app |

  **Do NOT open 5432 or 6379.** They must never be reachable externally.
- Attach a **static IP** and point your domain's **DNS A record** at it.
  TLS issuance fails until DNS resolves to the instance.

## 2. Install Docker on the host

```bash
ssh ubuntu@<STATIC_IP>
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# log out/in so the group takes effect
exit
```

## 3. Get the code

```bash
ssh ubuntu@<STATIC_IP>
git clone https://github.com/Commoditycrm/options-trading.git
cd options-trading
git checkout deploy/lightsail-selfhosted   # until merged to main
```

## 4. Create the `.env`

```bash
cp .env.prod.example .env
nano .env
```

Fill every `CHANGE_ME`. Generate secrets on the box:

```bash
# Postgres + Redis passwords (run twice, use distinct values)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# JWT secret
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
# Fernet credential-encryption key (exact format — do not hand-roll)
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set `DOMAIN`, `PUBLIC_URL`, `CORS_ORIGINS`, `FRONTEND_BASE_URL` to your real
domain. **Never rotate `CREDENTIAL_ENCRYPTION_KEY` or `JWT_SECRET` in place**
once live — it invalidates stored broker credentials / all sessions.

## 5. Build and start

```bash
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f caddy backend
```

Caddy obtains the Let's Encrypt cert automatically on first request once DNS
points at the box. Migrations (`alembic upgrade head`) run inside the backend
container at startup.

## 6. Verify

```bash
curl -fsS https://<DOMAIN>/api/health          # -> healthy JSON
```

Open `https://<DOMAIN>` and run the first-run flow from the README
(register the single trader account first, then subscribers).

Confirm the data layer is **not** externally reachable (run from your laptop,
NOT the box) — both should hang/refuse:

```bash
nc -vz <STATIC_IP> 5432
nc -vz <STATIC_IP> 6379
```

## Operations

```bash
# Update to latest code
git pull && docker compose -f docker-compose.prod.yml up -d --build

# Tail logs / restart one service
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml restart backend

# Backup Postgres
docker compose -f docker-compose.prod.yml exec db \
  pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backup_$(date +%F).sql

# Inspect the fanout stream
docker compose -f docker-compose.prod.yml exec redis \
  redis-cli -a "$REDIS_PASSWORD" XINFO STREAM signalboxx:fanout
```

## Notes

- **Backups:** `pgdata` and `redisdata` are named volumes that survive
  `down`/`up`. Snapshot the Lightsail instance regularly and/or cron the
  `pg_dump` above off-box.
- **Scaling the worker:** `docker compose -f docker-compose.prod.yml up -d
  --scale worker=3`. All workers share one Redis consumer group, so messages
  split across them without duplication.
- **No managed DB:** you own Postgres/Redis durability here. If you later want
  managed data (Neon + Redis Cloud), point `DATABASE_URL`/`REDIS_URL` at them
  in `.env` and drop the `db`/`redis` services from the compose file.
