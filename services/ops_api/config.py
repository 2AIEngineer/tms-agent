"""Configuration de l'API d'exploitation."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class OpsApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql://postgres:admin@localhost:5432/tms_agent_db",
        alias="DATABASE_URL",
    )
    mcp_server_url: str = Field(default="http://127.0.0.1:8010/mcp", alias="MCP_SERVER_URL")

    host: str = Field(default="127.0.0.1", alias="OPS_API_HOST")
    port: int = Field(default=8020, alias="OPS_API_PORT")
    # Origines autorisées pour le frontend. Explicites : un `*` avec jetons
    # porteurs laisserait n'importe quelle page lire les alertes d'un opérateur
    # déjà connecté.
    cors_origins: tuple[str, ...] = Field(default=("http://localhost:5173",), alias="OPS_API_CORS_ORIGINS")

    # --- Microsoft Entra ID (OAuth2/OIDC) ---
    entra_tenant_id: str = Field(default="", alias="ENTRA_TENANT_ID")
    # Identifiant d'audience de l'API, tel que déclaré dans Entra. Un jeton
    # émis pour une autre application doit être refusé, même signé par le bon
    # tenant : sans cette vérification, n'importe quelle application du tenant
    # pourrait appeler cette API.
    entra_audience: str = Field(default="", alias="ENTRA_AUDIENCE")
    jwks_cache_seconds: int = Field(default=3600, ge=60, alias="ENTRA_JWKS_CACHE_SECONDS")

    # Mode développement : aucune vérification de jeton, un opérateur fictif.
    # Explicite et bruyant à dessein — il n'y a pas de tenant Entra sur le
    # poste de développement, et ce drapeau ne doit jamais passer en production
    # par inadvertance.
    auth_disabled: bool = Field(default=True, alias="OPS_API_AUTH_DISABLED")
    dev_operator_id: str = Field(default="dev_operator", alias="OPS_API_DEV_OPERATOR")
    dev_roles: tuple[str, ...] = Field(default=("ops_supervisor",), alias="OPS_API_DEV_ROLES")

    # --- Diffusion temps réel ---
    # Intervalle de relecture des nouvelles alertes. Deux secondes : au-delà,
    # un opérateur a l'impression que l'interface est figée ; en deçà, on
    # interroge la base pour rien.
    sse_poll_interval_s: float = Field(default=2.0, gt=0, alias="OPS_API_SSE_POLL_INTERVAL_S")
    # Battement envoyé même sans alerte : sans lui, un mandataire coupe une
    # connexion inactive et l'interface croit le flux vivant.
    sse_keepalive_s: float = Field(default=15.0, gt=0, alias="OPS_API_SSE_KEEPALIVE_S")

    @property
    def jwks_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"


@lru_cache
def get_settings() -> OpsApiSettings:
    return OpsApiSettings()
