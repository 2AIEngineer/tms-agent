"""Objets du cycle d'envoi des notifications."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from shared.mapped_dto import NotificationChannel, NotificationStatus, Severity


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """Une demande d'envoi réclamée dans la file, prête à être traitée.

    Porte le contenu de l'alerte (titre, message, gravité) parce que le message
    est figé au moment de la détection : une alerte dont la règle change de
    seuil ne doit pas voir son SMS déjà parti se réécrire.
    """

    id: uuid.UUID
    alert_id: uuid.UUID
    tms_id: str
    mission_id: str
    role: str | None
    user_id: str | None
    management_level: str
    channel: NotificationChannel
    attempts: int
    severity: Severity
    title: str
    message: str

    @property
    def target_label(self) -> str:
        return self.role or f"user:{self.user_id}"


@dataclass(frozen=True, slots=True)
class SentNotification:
    """Résultat d'envoi pour un destinataire, tel qu'il est enregistré."""

    outbox_id: uuid.UUID
    alert_id: uuid.UUID
    channel: NotificationChannel
    management_level: str
    recipient_id: str
    recipient_name: str | None
    recipient_role: str | None
    address: str | None
    status: NotificationStatus
    failure_reason: str | None
    provider: str | None
    provider_reference: str | None
    sent_at: datetime


@dataclass(slots=True)
class DispatchReport:
    """Bilan d'un passage du consommateur de file."""

    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    rescheduled: int = 0
    abandoned: int = 0
    no_recipient: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"demandes={self.claimed} envois_ok={self.delivered} envois_echoues={self.failed} "
            f"reprogrammees={self.rescheduled} abandonnees={self.abandoned}"
        )
