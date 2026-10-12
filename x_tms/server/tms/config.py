from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVER_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuration, surchargeable par variables d'environnement préfixées `TMS_`."""

    model_config = SettingsConfigDict(env_prefix="TMS_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{SERVER_DIR / 'data' / 'tms.db'}"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Écart (km) en dessous duquel le véhicule est considéré sur l'itinéraire.
    on_route_tolerance_km: float = 0.5
    # Au-delà de ce délai sans position, un véhicule est considéré `offline`.
    offline_after_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
