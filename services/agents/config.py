"""Configuration du service agent."""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.mapped_dto import Severity

from .assistant.schedule import ReportSchedule
from .integrations.budget import DEFAULT_MAX_INPUT_TOKENS
from .integrations.litellm_client import DEFAULT_MAX_OUTPUT_TOKENS
from .operations.driver_call import CallGuardrails

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AgentSettings(BaseSettings):
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

    # --- Inférence (F19 à F22) ---
    litellm_base_url: str = Field(default="http://localhost:4000", alias="LITELLM_BASE_URL")
    litellm_api_key: str = Field(default="", alias="LITELLM_MASTER_KEY")
    # Nom **logique** du modèle : le routage, le repli et les quotas vivent dans
    # la configuration de LiteLLM. Changer de fournisseur ne touche pas ce code.
    model: str = Field(default="mission-investigator", alias="AGENT_MODEL")
    # Mode raisonnement au minimum pour l'enquêteur : ces jetons sont facturés
    # en sortie et n'apportent rien à une analyse déjà structurée.
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    request_timeout_s: float = Field(default=120.0, gt=0)
    # Plafond de jetons produits par tour. Sans lui, le modèle peut générer
    # jusqu'au bout de la fenêtre : mesuré, une conclusion d'enquête passait de
    # quinze secondes à plus de dix minutes sur une fenêtre de 32 768.
    max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=128, alias="AGENT_MAX_OUTPUT_TOKENS")

    # --- Enquêtes ---
    investigate_min_severity: Severity = Field(default=Severity.MEDIUM)
    # F20 : deux enquêtes en parallèle, pour que la détection n'attende jamais
    # le LLM. Au-delà, le gain est absorbé par les quotas du fournisseur.
    max_concurrent_investigations: int = Field(default=2, ge=1)
    # F19 : au plus quatre par passage, les plus graves d'abord.
    investigations_per_batch: int = Field(default=4, ge=1)
    # Tours de boucle d'un enquêteur avant abandon. Un plafond est indispensable :
    # un LLM qui ne conclut pas consommerait indéfiniment des jetons.
    max_tool_rounds: int = Field(default=8, ge=1)
    # Une enquête échouée est reprise, mais pas indéfiniment : passé ce seuil,
    # l'alerte reste sans analyse, ce qui est préférable à une boucle d'échecs.
    max_attempts: int = Field(default=3, ge=1)
    poll_interval_s: int = Field(default=5, ge=1)
    # Répertoire des règles, pour les **réévaluer** pendant une enquête (F21).
    # Le même que celui du worker : deux jeux de règles divergents feraient
    # juger l'enquête sur une règle qui n'a pas créé l'alerte.
    rules_directory: Path = Field(
        default=PROJECT_ROOT / "services/monitoring/rules/definitions",
        alias="RULES_DIRECTORY",
    )

    # --- Appel au chauffeur (F21, F26) ---
    # La seule action du système vers l'extérieur, donc la seule dont les
    # limites sont configurables : un exploitant doit pouvoir les resserrer
    # sans toucher au code.
    driver_call_enabled: bool = Field(default=True, alias="DRIVER_CALL_ENABLED")
    # Un appel dérange un conducteur : en deçà de ce seuil, une notification
    # in-app suffit. Distinct du seuil d'enquête, qui ne dérange personne.
    driver_call_min_severity: Severity = Field(default=Severity.HIGH, alias="DRIVER_CALL_MIN_SEVERITY")
    driver_call_max_per_mission: int = Field(default=3, ge=1, alias="DRIVER_CALL_MAX_PER_MISSION")
    driver_call_max_per_driver: int = Field(default=2, ge=1, alias="DRIVER_CALL_MAX_PER_DRIVER")
    driver_call_window_minutes: int = Field(default=60, ge=1, alias="DRIVER_CALL_WINDOW_MINUTES")
    driver_call_cooldown_minutes: int = Field(default=15, ge=0, alias="DRIVER_CALL_COOLDOWN_MINUTES")

    # Plafond de contexte envoyé au modèle, marge de sortie comprise. Mesuré :
    # le modèle de développement a une fenêtre de 4 096 jetons, et un troisième
    # tour d'enquête la dépassait — le fournisseur refusait alors la requête
    # entière. Le plafond vaut aussi pour le repli cloud, dont le quota par
    # minute est l'autre limite rencontrée en pratique.
    max_input_tokens: int = Field(default=DEFAULT_MAX_INPUT_TOKENS, ge=512, alias="AGENT_MAX_INPUT_TOKENS")

    # --- Validation humaine des actions de l'assistant ---
    # Durée de vie d'une action proposée. Assez longue pour qu'un opérateur
    # interrompu par un appel revienne la valider, assez courte pour qu'un
    # accord ne survive pas à la situation qui le justifiait.
    assistant_action_ttl_minutes: int = Field(default=15, ge=1, alias="ASSISTANT_ACTION_TTL_MINUTES")

    @property
    def assistant_action_ttl(self) -> timedelta:
        return timedelta(minutes=self.assistant_action_ttl_minutes)

    # --- Rapport de situation à heure fixe (F24) ---
    # Désactivé par défaut : les heures dépendent de l'exploitation du client,
    # et un rapport produit à une heure qui n'a pas de sens n'est pas lu.
    report_enabled: bool = Field(default=False, alias="ASSISTANT_REPORT_ENABLED")
    report_times: tuple[str, ...] = Field(default=("08:00", "14:00"), alias="ASSISTANT_REPORT_TIMES")
    # Fuseau de l'exploitation : « 8 h » doit rester 8 h pour l'agent de suivi,
    # y compris après un changement d'heure.
    report_timezone: str = Field(default="Europe/Paris", alias="ASSISTANT_REPORT_TIMEZONE")
    report_operator_id: str = Field(default="scheduled_report", alias="ASSISTANT_REPORT_OPERATOR")
    report_tms_ids: tuple[str, ...] = Field(default=(), alias="ASSISTANT_REPORT_TMS_IDS")
    # Un créneau manqué de plus longtemps est abandonné : un rapport de la
    # veille n'intéresse personne.
    report_catch_up_minutes: int = Field(default=30, ge=1, alias="ASSISTANT_REPORT_CATCH_UP_MINUTES")

    def report_schedule(self) -> "ReportSchedule":
        return ReportSchedule.parse(
            self.report_times,
            timezone=self.report_timezone,
            catch_up_minutes=self.report_catch_up_minutes,
        )

    def guardrails(self) -> "CallGuardrails":
        return CallGuardrails(
            max_per_mission=self.driver_call_max_per_mission,
            max_per_driver_per_window=self.driver_call_max_per_driver,
            driver_window_minutes=self.driver_call_window_minutes,
            driver_cooldown_minutes=self.driver_call_cooldown_minutes,
            min_severity=self.driver_call_min_severity,
        )


@lru_cache
def get_settings() -> AgentSettings:
    return AgentSettings()
