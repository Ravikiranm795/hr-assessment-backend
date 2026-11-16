"""Environment-driven settings. The only place product and vendor names may live."""

import re
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_APP_NAME = "HR Assessment Platform"
DEFAULT_VENDOR_NAME = "MRK Innovations"

CommaList = Annotated[list[str], NoDecode]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# Stable identifiers: they default to the values the original default name produced, so renaming
# APP_NAME never invalidates issued tokens or refresh cookies (override only to match older deployments).
DEFAULT_JWT_ISSUER = _slug(DEFAULT_APP_NAME)
DEFAULT_REFRESH_COOKIE_NAME = f"{DEFAULT_JWT_ISSUER}_refresh"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore",
        validate_by_name=True,
        validate_by_alias=True,
    )

    # Branding: the single source for the product and vendor names.
    app_name: str = DEFAULT_APP_NAME
    vendor_name: str = Field(default=DEFAULT_VENDOR_NAME, validation_alias="APP_VENDOR_NAME")

    app_env: Literal["dev", "test", "prod"] = "dev"
    api_base_url: str = "http://localhost:8000"
    app_base_url: str = "http://localhost:3000"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/hr_assessment"
    database_url_reports_ro: str = ""
    # "maintenance" role connection for audit partition DDL (job audit_partitions); else DATABASE_URL.
    database_url_maintenance: str = ""
    # Runtime DB roles. tenant_session()/reports_session() run SET LOCAL ROLE so RLS applies even when
    # the connection user is a superuser (dev/compose). Empty string disables (connect as the role directly).
    db_app_role: str = "app_rw"
    db_reports_role: str = "reports_ro"

    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_kid: str = "k1"
    # Token issuer claim. Independent of APP_NAME on purpose (a rename must not log everyone out).
    jwt_issuer: str = DEFAULT_JWT_ISSUER
    refresh_cookie_name: str = DEFAULT_REFRESH_COOKIE_NAME
    access_ttl_min: int = 15
    refresh_ttl_days: int = 7

    # Format is owned by app.core.security: comma-separated "<key_id>:<base64 32-byte key>".
    field_encryption_keys: str = ""
    token_pepper: str = ""

    # Argon2id cost, calibrated to about 250 ms per hash in the API container (see core/README.md).
    argon2_time_cost: int = 12
    argon2_memory_kib: int = 65536
    argon2_parallelism: int = 4

    default_sa_email: str = ""
    default_sa_name: str = ""
    default_sa_password: str = ""

    storage_driver: Literal["local", "s3"] = "local"
    storage_local_path: str = "/app/storage_data"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = ""

    google_client_id: str = ""

    cors_origins: CommaList = ["http://localhost:3000"]
    cookie_domain: str = ""
    cookie_secure: bool = False

    ai_cli_allowlist: CommaList = ["claude"]
    ai_cli_timeout_sec: int = 120
    ai_request_timeout_sec: int = 60

    max_upload_mb: int = 5
    max_snapshot_kb: int = 300
    max_jd_chars: int = 8000

    # Rate limits (spec 4.7), slowapi "<count>/<period>" strings unless noted.
    rate_limit_login: str = "10/minute"
    rate_limit_exam_link: str = "20/minute"
    rate_limit_exam_events: str = "120/minute"
    rate_limit_ai_generate: str = "5/minute"
    rate_limit_report_interpret: str = "10/minute"
    rate_limit_invite_email: str = "60/hour"
    rate_limit_snapshot_interval_sec: int = 20
    rate_limit_snapshot_max_per_attempt: int = 300

    log_level: str = "INFO"
    demo_mode: bool = False

    @field_validator("cors_origins", "ai_cli_allowlist", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def app_slug(self) -> str:
        """Lowercase, hyphenated form of the name (display only; the JWT issuer is ``jwt_issuer``)."""
        return _slug(self.app_name)

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        if self.app_env == "prod":
            missing = [
                name
                for name in ("token_pepper", "field_encryption_keys", "jwt_private_key", "jwt_public_key")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(f"missing required settings in prod: {', '.join(missing)}")
        return self


settings = Settings()
