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
