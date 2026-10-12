"""Envoi des notifications d'alerte.

    Alert  ->  notification_outbox  ->  résolution des destinataires (MCP)
           ->  canal  ->  notifications (résultat par destinataire)

La mise en file se fait dans la transaction de l'alerte ; l'envoi est
asynchrone, pour qu'un fournisseur lent ne ralentisse jamais la surveillance.
"""

from .models import DispatchReport, OutboxEntry, SentNotification
from .outbox import OutboxRepository
from .service import NotificationService

__all__ = [
    "DispatchReport",
    "NotificationService",
    "OutboxEntry",
    "OutboxRepository",
    "SentNotification",
]
