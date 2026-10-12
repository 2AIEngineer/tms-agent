"""Configuration du serveur MCP, frontière d'intégration avec le TMS."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.monitoring.config import PROJECT_ROOT


class McpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Accepte le nom du champ **et** son alias d'environnement. Sans cela,
        # construire les réglages en Python avec le nom du champ serait ignoré
        # en silence — `extra="ignore"` ne lèverait même pas.
        populate_by_name=True,
    )

    # --- TMS ---
    # Identifiant de la connexion : il cantonne la déduplication des alertes et
    # l'idempotence des événements. Le changer sur un système en service
    # détacherait les alertes ouvertes de leurs missions.
    tms_id: str = Field(default="x_tms", alias="TMS_ID")
    tms_dialect: str = Field(default="x_tms", alias="TMS_DIALECT")
    # Fichier de connexions, pour raccorder plusieurs TMS. Absent, la connexion
    # unique est décrite par les variables ci-dessous.
    tms_connections_file: Path | None = Field(default=None, alias="TMS_CONNECTIONS_FILE")
    tms_base_url: str = Field(default="http://localhost:8000", alias="TMS_BASE_URL")
    tms_api_prefix: str = Field(default="/v1")
    tms_api_key: str | None = Field(default=None, alias="TMS_API_KEY")

    # Un délai court : le cycle de surveillance tourne toutes les 10 s et lit
    # plusieurs ressources par mission. Une requête qui traîne 30 s ferait
    # déborder le cycle au lieu d'échouer proprement et d'être signalée (F4).
    tms_timeout_s: float = Field(default=5.0, gt=0)
    tms_connect_timeout_s: float = Field(default=2.0, gt=0)

    # Reprises sur erreur transitoire uniquement (délai dépassé, 429, 5xx).
    # Volontairement faible : mieux vaut signaler un cycle en échec et réessayer
    # au suivant que de s'acharner et décaler toute la boucle (F5).
    tms_max_retries: int = Field(default=2, ge=0)
    tms_retry_backoff_s: float = Field(default=0.25, gt=0)

    # --- Garde-fous vers le TMS ---
    # Le TMS est un système tiers que nous ne contrôlons pas : le saturer est le
    # risque principal de cette architecture par polling.
    tms_max_requests_per_second: float = Field(default=25.0, gt=0)
    tms_max_concurrent_requests: int = Field(default=8, ge=1)

    # --- Serveur MCP ---
    mcp_host: str = Field(default="127.0.0.1")
    mcp_port: int = Field(default=8010)
    # Transport HTTP streamable en mode sans état, pour autoriser plusieurs
    # réplicas derrière un même ingress.
    mcp_stateless: bool = Field(default=True)

    # Règles livrées : lues pour dire quelles règles un TMS rend inopérantes.
    # Le serveur ne les évalue pas — c'est le rôle du monitoring — il ne fait
    # que croiser leurs champs avec les capacités du TMS.
    rules_directory: Path = Field(default=PROJECT_ROOT / "services/monitoring/rules/definitions")

    @property
    def tms_api_root(self) -> str:
        return f"{self.tms_base_url.rstrip('/')}{self.tms_api_prefix}"


@lru_cache
def get_settings() -> McpSettings:
    return McpSettings()
