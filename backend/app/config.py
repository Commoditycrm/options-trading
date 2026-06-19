from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 30
    jwt_refresh_token_days: int = 14
    credential_encryption_key: str
    cors_origins: str = "http://localhost:3000"
    # Regex pattern matched against the Origin header. Anything matching this
    # is treated as an allowed origin, in addition to cors_origins above.
    # Default covers Vercel's "production alias" + "<commit>-<team>" preview
    # URLs for any project named options-trading-*. Override per environment.
    cors_origin_regex: str = r"https://options-trading.*\.vercel\.app"
    frontend_base_url: str = "http://localhost:3000"

    # IBKR Web API (OAuth 1.0a 3rd-party flow). All four are app-level —
    # shared across every user's BrokerAccount. Each user supplies their
    # own access_token + access_token_secret via the OAuth dance, stored
    # encrypted on the BrokerAccount row.
    # Leave blank to disable IBKR (the broker option will surface in the
    # UI but connection attempts return 501).
    ibkr_consumer_key: str = ""
    ibkr_dh_param_pem: str = ""           # PEM contents (multiline) — not a file path
    ibkr_private_encryption_pem: str = ""
    ibkr_private_signature_pem: str = ""

    # SnapTrade aggregator (official SDK). App-level credentials shared across
    # all users — get them from https://dashboard.snaptrade.com/. Per-user
    # userSecret is obtained at connect time and stored encrypted on the
    # BrokerAccount. Leave blank to disable SnapTrade: the connect endpoints
    # return a clean 503 instead of failing with an opaque SDK auth error.
    snaptrade_client_id: str = ""
    snaptrade_consumer_key: str = ""
    # When True, SnapTrade pushes order updates to our webhook instead of us
    # polling (near-instant detection). Requires a publicly reachable webhook
    # URL configured in the SnapTrade dashboard.
    snaptrade_webhook_enabled: bool = False

    # Redis for fanout work-queue (Streams + Consumer Groups, NOT pub/sub —
    # we need one message per worker, not broadcast). Set REDIS_URL to a
    # full redis:// or rediss:// URL. Leave blank to disable Redis-based
    # fanout entirely; in that case copy_engine.fanout runs the existing
    # in-process ThreadPoolExecutor path (fine for single-pod dev).
    redis_url: str = ""
    fanout_stream: str = "optionhaven:fanout"     # XADD stream name
    fanout_group: str = "fanout_workers"          # consumer group

    # When True, the FastAPI process also starts fanout workers as
    # asyncio tasks at boot. Convenient for local dev (no second process to
    # run). Production should set this to False and run worker.py as a
    # separate Render service so the worker pool can scale independently
    # of the backend pod's memory budget.
    run_fanout_worker_in_process: bool = True

    # App 2's signature path: route detected trader orders through the
    # queue-based fast fanout (queue_fanout + pending_copies + async worker
    # pool) instead of the legacy serial fanout / Redis Streams. Default True.
    # Set False to fall back to the legacy dispatch (for A/B comparison).
    use_queue_fanout: bool = True

    # Fan-out strategy selector for the hot path (trade-panel + external
    # detection). Phase 1 of the latency rewrite introduces "inproc": an
    # in-process BATCHED fan-out (copy_engine.fanout_inproc) that builds every
    # child Order, does ONE flush, fires the broker calls in parallel, then does
    # ONE commit — collapsing the queue path's ~2-commits-per-copy storm into
    # two round-trips. Values:
    #   "queue"  → queue_fanout + pending_copies + async worker pool (DEFAULT;
    #              keeps the existing path + LISTEN/NOTIFY fallback intact).
    #   "inproc" → fanout_inproc batched in-process fan-out (flip on the box to
    #              latency-test; full gate parity with the worker path).
    # NOTE: when use_queue_fanout=False this is ignored — the legacy serial /
    # Redis dispatch wins (see dispatch_detected_order). Default keeps queue so
    # nothing changes until explicitly flipped.
    fanout_mode: str = "queue"

    # Number of worker threads to spawn. Each one runs its own consume_loop
    # against the same Redis Stream Consumer Group, so messages are shared
    # across them — true parallel processing of subscriber broker calls.
    # Default 8 fits comfortably in 512MB (Render free/Starter). Bump
    # higher when running the dedicated worker service with more RAM:
    # ~30-50MB per worker, so 1GB → 20 workers, 2GB → 50 workers.
    # Bigger than ~50 hits diminishing returns (broker rate limits +
    # Postgres connection pool contention become the bottleneck).
    fanout_worker_count: int = 8

    # ── SMTP for transactional email (password reset). Leave smtp_host blank
    # to disable real sending — the email service then logs the reset link
    # instead (dev/staging fallback so the flow is testable without a relay).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "no-reply@optionhaven.app"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ibkr_configured(self) -> bool:
        """True if all four app-level IBKR signing artefacts are set."""
        return all([
            self.ibkr_consumer_key,
            self.ibkr_dh_param_pem,
            self.ibkr_private_encryption_pem,
            self.ibkr_private_signature_pem,
        ])


@lru_cache
def get_settings() -> Settings:
    return Settings()
