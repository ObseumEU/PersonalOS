from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from POS_* environment variables or a .env file."""

    model_config = SettingsConfigDict(env_prefix="POS_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    # Single-user login. Empty password disables login (only for local dev).
    password: str = ""
    session_secret: str = "dev-only-change-me"
    # Send the session cookie only over HTTPS (set true on the server).
    secure_cookies: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "personalos.db"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


@lru_cache
def get_settings() -> Settings:
    return Settings()
