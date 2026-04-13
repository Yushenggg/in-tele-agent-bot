from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Telegram
    telegram_token: str = Field(..., alias="TELEGRAM_TOKEN")
    admin_users: list[int] = Field(default_factory=list, alias="ADMIN_USERS")
    allowed_users: list[int] = Field(default_factory=list, alias="ALLOWED_USERS")

    # Agent
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    default_model: str | None = Field(default=None, alias="DEFAULT_MODEL")

    # Session
    default_idle_timeout_seconds: int = Field(
        default=300, alias="DEFAULT_IDLE_TIMEOUT_SECONDS"
    )

    # Database
    database_path: str = Field(default="./telebasebot.db", alias="DATABASE_PATH")

    # Logging
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="console", alias="LOG_FORMAT")

    @property
    def openai_base_url_for_client(self) -> str | None:
        if self.openai_base_url is None:
            return None
        normalized = self.openai_base_url.rstrip("/")
        if normalized.endswith("/v1"):
            return normalized
        return f"{normalized}/v1"


app_config = AppConfig()
