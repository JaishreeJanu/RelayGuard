from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/relayguard"
    )
    app_name: str = "RelayGuard"
    redis_url: str = "redis://localhost:6379/0"
    mock_vendor_url: str = "http://localhost:8001/mock/send"

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async engine URL (asyncpg driver)."""
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        return self.database_url

    def sync_database_url(self) -> str:
        """Alembic / psycopg2 URL derived from database_url."""
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url


settings = Settings()
