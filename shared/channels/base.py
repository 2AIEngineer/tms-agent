"""Contrat d'un canal d'envoi.

C'est la couture par laquelle un fournisseur réel — Twilio, Infobip, Azure
Communication Services, SendGrid — se branchera. Tant qu'aucun contrat
fournisseur n'est signé, seul le canal de développement existe, et le reste du
système ignore la différence.

Un échec d'envoi est **rendu**, jamais levé : le service de notification doit
enregistrer chaque tentative avec son statut (F17), et une exception ne se
range pas dans une colonne. La distinction qui compte ici est celle entre un
échec **permanent** — un superviseur sans numéro de téléphone, que réessayer
n'arrangera jamais — et un échec **transitoire** : fournisseur indisponible,
quota atteint.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from shared.mapped_dto import NotificationChannel, Severity


@dataclass(frozen=True, slots=True)
class Message:
    """Ce qui part réellement vers un destinataire."""

    channel: NotificationChannel
    address: str | None
    title: str
    body: str
    severity: Severity
    # Contexte repris dans les journaux du fournisseur et dans l'audit : permet
    # de relier un SMS reçu à l'alerte qui l'a provoqué.
    alert_id: str
    mission_id: str
    recipient_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Résultat d'une tentative d'envoi."""

    delivered: bool
    provider: str
    reference: str | None = None
    failure_reason: str | None = None
    # Faux pour un échec définitif : destinataire injoignable, numéro invalide.
    # Vrai pour un incident passager, qui mérite une nouvelle tentative.
    retryable: bool = False

    @classmethod
    def ok(cls, provider: str, reference: str | None = None) -> "SendResult":
        return cls(delivered=True, provider=provider, reference=reference)

    @classmethod
    def permanent_failure(cls, provider: str, reason: str) -> "SendResult":
        return cls(delivered=False, provider=provider, failure_reason=reason, retryable=False)

    @classmethod
    def transient_failure(cls, provider: str, reason: str) -> "SendResult":
        return cls(delivered=False, provider=provider, failure_reason=reason, retryable=True)


class Channel(Protocol):
    """Un canal capable d'acheminer un message.

    Un protocole et non une classe de base : un adaptateur fournisseur n'a rien
    à hériter, il a seulement à savoir envoyer.
    """

    name: str

    @property
    def channel(self) -> NotificationChannel: ...

    async def send(self, message: Message, *, sent_at: datetime) -> SendResult: ...
