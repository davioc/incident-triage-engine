from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Root directory of the repository (2 levels up from src/config.py)
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "Incident Triage Engine"
    TRIAGE_MODE: str = "mock"

    # Single source of truth for the version shown by FastAPI, /api/v1/health and
    # the dashboard badge. Bump the minor number for new endpoints or UI
    # capabilities, the patch number for fixes. Screenshots are named after the
    # release they document.
    VERSION: str = "1.2.0"

    # Paths are anchored to the project root and overridable at runtime, so a container
    # can point the index at a mounted volume instead of the image's /app tree.
    # CHROMA_DB_DIR is read when the store is built, not at import time.
    RUNBOOKS_DIR: Path = BASE_DIR / "docs" / "runbooks"
    CHROMA_DB_DIR: Path = BASE_DIR / ".chroma_db"

    # Report storage is opt-in: an empty value means storage is disabled rather than
    # a half-configured database. Compose sets a postgres URL; local runs without a
    # database keep working offline. See docs/adr/0002-database-selection.md.
    DATABASE_URL: str = ""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
