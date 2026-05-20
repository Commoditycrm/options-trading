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

    # Redis for fanout work-queue (Streams + Consumer Groups, NOT pub/sub —
    # we need one message per worker, not broadcast). Set REDIS_URL to a
    # full redis:// or rediss:// URL. Leave blank to disable Redis-based
    # fanout entirely; in that case copy_engine.fanout runs the existing
    # in-process ThreadPoolExecutor path (fine for single-pod dev).
    redis_url: str = ""
    fanout_stream: str = "signalboxx:fanout"     # XADD stream name
    fanout_group: str = "fanout_workers"          # consumer group

    # When True, the FastAPI process also starts a fanout worker as an
    # asyncio task at boot. Convenient for local dev (no second process to
    # run). Production should set this to False and run worker.py as a
    # separate Render service so the worker can scale independently.
    run_fanout_worker_in_process: bool = True

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
