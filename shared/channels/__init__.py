"""Canaux d'envoi des notifications.

Un canal sait acheminer un message vers une adresse ; il ne connaît ni les
alertes, ni les règles, ni les destinataires. Le service de notification résout
le destinataire, choisit le canal et enregistre le résultat.

En développement, les quatre canaux sont servis par `ConsoleChannel` : aucun
fournisseur n'est contractualisé, et le POC tourne sur des numéros fictifs.
Brancher un fournisseur consiste à écrire une classe conforme au protocole
`Channel` et à l'inscrire dans le registre — sans toucher au service.
"""

from shared.mapped_dto import NotificationChannel

from .base import Channel, Message, SendResult
from .console import ConsoleChannel


def console_registry() -> dict[NotificationChannel, Channel]:
    """Registre de développement : les quatre canaux, tous en mode console."""
    return {channel: ConsoleChannel(channel) for channel in NotificationChannel}


__all__ = [
    "Channel",
    "ConsoleChannel",
    "Message",
    "SendResult",
    "console_registry",
]
