"""Environment-based settings."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    postgres_port: int
    redis_host: str
    redis_port: int
    replica_id: str

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}"


def load_settings() -> Settings:
    return Settings(
        postgres_user=os.environ.get("POSTGRES_USER", "pingraph"),
        postgres_password=os.environ.get("POSTGRES_PASSWORD", "pingraph"),
        postgres_db=os.environ.get("POSTGRES_DB", "pingraph"),
        postgres_host=os.environ.get("POSTGRES_HOST", "localhost"),
        postgres_port=int(os.environ.get("POSTGRES_PORT", "5433")),
        redis_host=os.environ.get("REDIS_HOST", "localhost"),
        redis_port=int(os.environ.get("REDIS_PORT", "6379")),
        replica_id=os.environ.get("REPLICA_ID", "local"),
    )


settings = load_settings()
