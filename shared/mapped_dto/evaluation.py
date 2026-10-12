"""Résultat d'évaluation du moteur de règles.

Le moteur ne crée pas d'alerte : il rend ce qu'il a observé. Le cycle de vie
(déduplication, cooldown, clôture) appartient à l'Alert Service. C'est cette
coupure qui rend les règles testables sans base de données ni notification.

    MissionSnapshot -> Rules Engine -> RuleEvaluation -> Alert Service
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .alert import NotificationRequest, Severity


class RuleAction(StrEnum):
    EMIT_ALERT = "emit_alert"
    LOG_ONLY = "log_only"
    CREATE_REPORT = "create_report"


class RuleEvaluation(BaseModel):
    """Verdict d'une règle sur une mission, à un instant TMS donné.

    Les évaluations non déclenchées sont renvoyées elles aussi : une règle qui
    cesse d'être vraie est ce qui permet de clore automatiquement l'alerte
    correspondante (F13).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_version: int
    mission_id: str

    matched: bool
    action: RuleAction | None = None

    severity: Severity | None = None
    category: str | None = None

    title: str | None = None
    message: str | None = None
    deduplication_key: str | None = None
    cooldown_minutes: int = 0
    auto_resolve: bool = True

    observed_data: dict[str, Any] = Field(default_factory=dict)
    notify: tuple[NotificationRequest, ...] = ()

    event_id: str | None = Field(
        default=None,
        description="Renseigné pour une règle d'événement : l'événement qui l'a déclenchée.",
    )

    evaluated_at: datetime = Field(description="Heure TMS de l'évaluation, jamais l'horloge locale.")
