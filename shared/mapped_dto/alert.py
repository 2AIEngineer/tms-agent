"""Alerte, notification et sévérité : le résultat métier de la détection."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Catégories utilisées par les règles livrées. La liste est indicative et non
# contraignante : `Alert.category` reste une chaîne libre, parce que l'équipe
# métier doit pouvoir introduire une catégorie en déposant un fichier YAML,
# sans livraison de code (objectif 7 de la spécification). Elle sert à
# l'affichage et aux filtres de l'interface.
KNOWN_CATEGORIES: tuple[str, ...] = (
    "route_deviation",
    "delay",
    "immobilization",
    "data_gap",
    "safety",
)


class Severity(StrEnum):
    """Gravité d'une alerte, du plus faible au plus grave.

    L'ordre est significatif : les déclenchements sont traités par sévérité
    décroissante (F9) et les enquêtes sont confiées au LLM les plus graves
    d'abord (F19). `rank` porte cet ordre, qu'une comparaison de chaînes ne
    donnerait pas.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank >= other.rank


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class AlertStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ResolutionReason(StrEnum):
    """Pourquoi une alerte a été close. Conservé pour l'audit (F27)."""

    CONDITION_CLEARED = "condition_cleared"
    MISSION_ENDED = "mission_ended"
    OPERATOR = "operator"


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"
    EMAIL = "email"
    CALL = "call"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class NotificationRequest(BaseModel):
    """Demande d'envoi, telle que la règle l'exprime.

    Le destinataire est désigné par son **rôle**, pas par son identité : il est
    résolu dans le TMS au moment de l'envoi (F16). Coder un identifiant en dur
    reviendrait à dupliquer l'annuaire du TMS dans nos règles.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str | None = None
    user_id: str | None = None
    management_level: str = Field(pattern=r"^M\+\d+$")
    channel: NotificationChannel


class Notification(BaseModel):
    """Envoi effectué, avec son statut. Un échec n'annule pas l'alerte (F17)."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    alert_id: uuid.UUID
    request: NotificationRequest
    resolved_recipient_id: str | None = None
    resolved_address: str | None = None
    status: NotificationStatus = NotificationStatus.PENDING
    failure_reason: str | None = None
    sent_at: datetime | None = None


class Alert(BaseModel):
    """Anomalie détectée par une règle sur une mission.

    `rule_version` est conservée avec l'alerte : sans elle, une alerte devient
    inexplicable dès que le seuil de sa règle change, et l'auditabilité promise
    à l'équipe métier tombe.

    Les dates sont en **heure TMS** : déduplication et cooldown se jugent sur
    le temps du TMS, jamais sur l'horloge locale (F11).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    # Provenance de la mission : c'est elle qui donne sa portée à la clé de
    # déduplication. Sans elle, deux TMS portant une mission du même
    # identifiant verraient l'alerte du second supprimée comme doublon.
    tms_id: str = Field(min_length=1)
    mission_id: str

    rule_id: str
    rule_version: int

    severity: Severity
    category: str

    title: str
    message: str

    observed_data: dict[str, Any] = Field(default_factory=dict)
    deduplication_key: str
    cooldown_minutes: int = 0
    auto_resolve: bool = True
    event_id: str | None = Field(
        default=None,
        description="Événement déclencheur, pour une alerte issue d'une règle d'événement.",
    )

    status: AlertStatus = AlertStatus.OPEN
    detected_at: datetime
    resolved_at: datetime | None = None
    resolution_reason: ResolutionReason | None = None
