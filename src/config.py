from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Root directory of the repository (2 levels up from src/config.py)
BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    PROJECT_NAME: str = "SRE Log Triage Engine"
    TRIAGE_MODE: str = "mock"

    # Path configuration anchored to project root
    RUNBOOKS_DIR: Path = BASE_DIR / "docs" / "runbooks"
    CHROMA_DB_DIR: Path = BASE_DIR / ".chroma_db"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = Settings()