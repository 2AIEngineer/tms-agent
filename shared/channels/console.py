"""Canal de développement : enregistre et affiche au lieu d'envoyer.

Aucun fournisseur de téléphonie ou d'e-mail n'est encore contractualisé, et le
POC tourne sur un TMS simulé avec des numéros fictifs. Envoyer réellement un
SMS à un numéro de test serait au mieux inutile, au pire facturé.

Ce canal est donc le comportement par défaut en développement. Deux rôles :

  - il **mémorise** les messages, ce qui permet aux tests de vérifier *ce qui
    serait parti* plutôt que de se contenter d'un statut en base ;
  - il **affiche** chaque notification en bloc lisible. Pendant une démonstration,
    le journal du worker est la seule fenêtre sur ce que le système envoie : une
    ligne unique de 200 caractères noyée entre deux requêtes HTTP ne permet pas
    de vérifier d'un coup d'œil que le bon interlocuteur a été joint sur le bon
    canal, au bon niveau de management.
"""

import logging
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from shared.mapped_dto import NotificationChannel

from .base import Message, SendResult

logger = logging.getLogger(__name__)

# Largeur du bloc. 88 colonnes : assez pour un message d'alerte sans déborder
# d'un terminal partagé avec les horodatages du journal.
BLOCK_WIDTH = 88

# Pictogrammes par canal. Ils servent au repérage visuel dans un flux de
# journal : à la relecture d'une démonstration, on cherche « qui a été appelé »
# plus souvent que « quelle ligne de log ».
CHANNEL_MARKS: dict[NotificationChannel, str] = {
    NotificationChannel.CALL: "APPEL  ",
    NotificationChannel.SMS: "SMS    ",
    NotificationChannel.EMAIL: "E-MAIL ",
    NotificationChannel.IN_APP: "IN-APP ",
}


@dataclass
class ConsoleChannel:
    """Canal qui journalise et mémorise les messages au lieu de les acheminer."""

    channel_type: NotificationChannel
    name: str = "console"
    sent: list[Message] = field(default_factory=list)
    # Permet aux tests de reproduire une panne de fournisseur sans dépendre
    # d'un vrai service indisponible.
    fail_with: SendResult | None = None

    @property
    def channel(self) -> NotificationChannel:
        return self.channel_type

    async def send(self, message: Message, *, sent_at: datetime) -> SendResult:
        if self.fail_with is not None:
            logger.warning(
                "%s",
                render_block(
                    message,
                    sent_at=sent_at,
                    status="NON ENVOYÉE",
                    failure_reason=self.fail_with.failure_reason,
                    retryable=self.fail_with.retryable,
                ),
            )
            return self.fail_with

        self.sent.append(message)
        reference = f"console-{uuid.uuid4().hex[:12]}"
        # Un seul appel au journal, et non un par ligne : deux envois
        # simultanés entrelaceraient sinon leurs blocs, et le résultat serait
        # moins lisible que la ligne unique qu'on cherche à remplacer.
        logger.info("%s", render_block(message, sent_at=sent_at, status="ENVOYÉE", reference=reference))
        return SendResult.ok(self.name, reference=reference)

    def clear(self) -> None:
        self.sent.clear()


def render_block(
    message: Message,
    *,
    sent_at: datetime,
    status: str,
    reference: str | None = None,
    failure_reason: str | None = None,
    retryable: bool | None = None,
) -> str:
    """Met une notification en forme sur plusieurs lignes alignées.

    Les champs sont ordonnés comme on les lit en exploitation : d'abord le canal
    et la gravité, puis *qui* est joint, et seulement ensuite le contenu. C'est
    l'ordre des questions qu'un superviseur se pose devant le journal.
    """
    mark = CHANNEL_MARKS.get(message.channel, message.channel.value.upper())
    header = f"{mark} {message.severity.value.upper()} · {status}"
    rule = "─" * max(0, BLOCK_WIDTH - len(header) - 4)

    role = message.metadata.get("role")
    level = message.metadata.get("management_level")
    destinataire = message.recipient_id
    if role:
        destinataire = f"{role} · {destinataire}"
    if level:
        destinataire = f"{destinataire} ({level})"

    lines = [
        f"┌─ {header} {rule}",
        _field("destinataire", destinataire),
        _field("adresse", _address(message)),
        _field("mission", message.mission_id),
        _field("alerte", message.alert_id),
        _field("heure", sent_at.isoformat(timespec="seconds")),
        _field("titre", message.title),
    ]
    lines.extend(_wrapped("message", message.body))

    if reference:
        lines.append(_field("référence", reference))
    if failure_reason:
        lines.append(_field("échec", failure_reason))
        if retryable is not None:
            lines.append(_field("", "à réessayer" if retryable else "définitif, reprise humaine"))

    lines.append("└" + "─" * (BLOCK_WIDTH - 1))
    return "\n" + "\n".join(lines)


def _address(message: Message) -> str:
    """Adresse de remise, ou la raison de son absence.

    La distinction compte : l'in-app n'a légitimement pas d'adresse, alors
    qu'un SMS sans numéro est une donnée manquante dans l'annuaire du TMS.
    Afficher la même mention pour les deux masquerait une anomalie.
    """
    if message.address:
        return message.address
    if message.channel is NotificationChannel.IN_APP:
        return "— (remise dans l'application)"
    return "— (aucune adresse dans le TMS)"


def _field(label: str, value: str) -> str:
    return f"│ {label:<13}{value}"


def _wrapped(label: str, value: str) -> list[str]:
    """Replie un texte long en conservant l'alignement de la colonne de valeurs."""
    width = BLOCK_WIDTH - 17
    pieces = textwrap.wrap(value, width=width) or [""]
    return [_field(label, pieces[0])] + [_field("", piece) for piece in pieces[1:]]
