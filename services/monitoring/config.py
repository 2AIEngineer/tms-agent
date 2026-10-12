"""Configuration du service de surveillance.

Les valeurs par défaut sont celles de la spécification : elles doivent pouvoir
être relues sans ouvrir le `.env`. Toute variable est surchargeable par
l'environnement, ce qui permet d'accélérer la boucle en développement sans
toucher au code.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.mapped_dto import Severity

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MonitoringSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # le .env est partagé par tous les services
        # Accepte le nom du champ **et** son alias d'environnement. Sans cela,
        # construire les réglages en Python avec le nom du champ serait ignoré
        # en silence — `extra="ignore"` ne lèverait même pas — et un test ou un
        # script croirait avoir configuré ce qu'il n'a pas configuré.
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql://postgres:admin@localhost:5432/tms_agent_db",
        alias="DATABASE_URL",
    )
    database_schema: str = Field(default="orchestrator", alias="POSTGRES_SCHEMA")

    # --- Boucle de surveillance (F1, F2) ---
    poll_interval_s: int = Field(default=10, ge=1, description="Durée d'un cycle de surveillance.")
    mission_concurrency: int = Field(
        default=6, ge=1, description="Instantanés de mission récupérés en parallèle."
    )
    cycle_backoff_max_s: int = Field(
        default=300, ge=1, description="Plafond du backoff exponentiel quand le TMS est injoignable (F5)."
    )

    # --- Enquêtes LLM (F19, F20) ---
    investigate_min_severity: Severity = Field(default=Severity.MEDIUM)
    investigations_per_cycle: int = Field(default=4, ge=0)

    # --- Notifications (F15 à F18) ---
    # Le serveur MCP est la seule porte vers le TMS, y compris pour résoudre
    # les destinataires au moment de l'envoi (F16).
    mcp_server_url: str = Field(default="http://127.0.0.1:8010/mcp", alias="MCP_SERVER_URL")

    notification_batch_size: int = Field(default=20, ge=1)
    # Durée du bail posé sur une demande réclamée. Doit couvrir confortablement
    # un envoi : trop court, une demande serait reprise par un autre expéditeur
    # alors que le premier est encore en train de l'acheminer.
    notification_lease_s: int = Field(default=60, ge=5)
    # Au-delà, la demande est abandonnée et laissée à une reprise humaine (F17).
    # Un échec de notification ne recrée jamais l'alerte.
    notification_max_attempts: int = Field(default=5, ge=1)
    notification_retry_backoff_s: int = Field(default=30, ge=1)
    notification_retry_backoff_max_s: int = Field(default=900, ge=1)

    # --- Règles ---
    rules_directory: Path = Field(default=PROJECT_ROOT / "services/monitoring/rules/definitions")

    # Base des identifiants de verrous consultatifs. La clé de chaque TMS en
    # est dérivée : c'est elle qui garantit qu'un TMS n'est surveillé que par
    # une instance à la fois, et qui sert de répartiteur entre instances.
    singleton_lock_key: int = Field(default=0x746D7301)

    # Nombre de TMS qu'un worker prend en charge. C'est ce plafond qui
    # transforme le verrou en répartiteur : sans lui, la première instance
    # prendrait tous les TMS et les suivantes n'auraient rien à faire.
    max_tms_per_worker: int = Field(default=8, ge=1, alias="MAX_TMS_PER_WORKER")

    # Restriction explicite à certains TMS, pour dédier un conteneur à un
    # client exigeant. Vide, le worker prend ce qu'il peut parmi tous.
    tms_allowlist: tuple[str, ...] = Field(default=(), alias="MONITORING_TMS_ALLOWLIST")

    # Le pool doit couvrir les boucles concurrentes : une connexion dédiée par
    # TMS surveillé (le verrou), plus les transactions de chaque cycle.
    database_pool_size: int = Field(default=10, ge=2, alias="DATABASE_POOL_SIZE")


@lru_cache
def get_settings() -> MonitoringSettings:
    """Configuration du processus, lue une seule fois."""
    return MonitoringSettings()
