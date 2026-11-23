"""Environment-based application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.exceptions import Bitrix24ConfigurationError


class Settings(BaseSettings):
    """Load local configuration without exposing secret values."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "CRM Consulting AI Platform — CRM Analyst"
    bitrix24_webhook_url: Optional[SecretStr] = None
    bitrix24_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    lead_hunter_api_token: Optional[SecretStr] = None
    lead_hunter_db: Path = Path('data/sales_team/universal.sqlite3')
    lead_hunter_output: Path = Path('outputs/lead_hunter/universal')
    bitrix24_open_channels_webhook_url: Optional[SecretStr] = None
    bitrix24_open_channels_app_token: Optional[SecretStr] = None
    bitrix24_open_channels_test_send: bool = False
    bitrix24_open_channels_test_chat_ids: set[int] = Field(default_factory=set)
    control_ai_enabled: bool = False
    control_gemini_key: Optional[SecretStr] = None
    control_ai_model: str = Field(default='gemini-flash-latest', pattern=r'^[a-zA-Z0-9.-]{1,100}$')
    control_ai_proxy: Optional[str] = None
    control_ai_daily_calls: int = Field(default=1000, ge=1, le=50000)
    control_secure_cookie: bool = True
    telephony_host: str = Field(default="185.22.64.70")
    telephony_ssh_user: str = Field(default="debian")
    telephony_caller_script: str = Field(default="/home/debian/bitrix_pbx/outbound_caller.py")
    meta_access_token: Optional[SecretStr] = None
    meta_phone_id: str = Field(default="1344487605411939")
    ads_seed_dir: Path = Path("data")
    ads_daily_call_cap: int = Field(default=200, ge=1, le=5000)

    @field_validator("bitrix24_webhook_url", mode="before")
    @classmethod
    def empty_webhook_is_missing(cls, value: object) -> object:
        """Treat an empty .env value as intentionally unconfigured."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_webhook_url(self) -> str:
        """Return the secret URL only where it is needed for a request."""

        if self.bitrix24_webhook_url is None:
            raise Bitrix24ConfigurationError(
                "BITRIX24_WEBHOOK_URL is missing from the local .env file."
            )

        webhook_url = self.bitrix24_webhook_url.get_secret_value().strip()
        if not webhook_url.startswith("https://"):
            raise Bitrix24ConfigurationError(
                "BITRIX24_WEBHOOK_URL must start with https://."
            )
        return webhook_url


@lru_cache
def get_settings() -> Settings:
    """Create settings once per application process."""

    return Settings()
