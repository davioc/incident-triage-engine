from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Root directory of the repository (2 levels up from src/config.py)
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "Incident Triage Engine"
    TRIAGE_MODE: str = "mock"

    # Paths are anchored to the project root and overridable at runtime, so a container
    # can point the index at a mounted volume instead of the image's /app tree.
    # CHROMA_DB_DIR is read when the store is built, not at import time.
    RUNBOOKS_DIR: Path = BASE_DIR / "docs" / "runbooks"
    CHROMA_DB_DIR: Path = BASE_DIR / ".chroma_db"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
