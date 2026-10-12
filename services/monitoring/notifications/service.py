"""Envoi des notifications d'alerte.

Consommateur de la file d'attente écrite par l'`AlertService`. Il résout les
destinataires dans le TMS au moment de l'envoi, achemine le message sur le
canal demandé et enregistre le résultat, destinataire par destinataire.

Les garanties portées par ce module :

  - **F16** : le destinataire est résolu à l'envoi, jamais figé à la détection.
    Un changement de numéro ou une rotation d'astreinte prend effet aussitôt.
  - **F17** : chaque envoi est enregistré avec son statut. Un échec ne recrée
    jamais l'alerte — il est journalisé pour reprise humaine.
  - **F18** : rien n'est envoyé pour une alerte supprimée par déduplication,
    puisque aucune demande n'a été mise en file pour elle.

Ce qui n'est **pas** fait ici : décider qui prévenir. Cela appartient aux
règles, et la décision est déjà figée dans la ligne de file.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from shared.channels import Channel, Message
from shared.mapped_dto import NotificationChannel, NotificationStatus
from shared.utils.db import Database

from ..integrations.mcp_client import McpCallFailed, McpClient, ResolvedRecipient
from .models import DispatchReport, OutboxEntry, SentNotification
from .outbox import OutboxRepository

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(
        self,
        database: Database,
        mcp: McpClient,
        channels: Mapping[NotificationChannel, Channel],
        *,
        batch_size: int = 20,
        lease_s: int = 60,
        max_attempts: int = 5,
        retry_backoff_s: int = 30,
        retry_backoff_max_s: int = 900,
        repository: OutboxRepository | None = None,
    ) -> None:
        self._database = database
        self._mcp = mcp
        self._channels = channels
        self._batch_size = batch_size
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._retry_backoff_s = retry_backoff_s
        self._retry_backoff_max_s = retry_backoff_max_s
        self._repository = repository or OutboxRepository()

    async def dispatch_once(self) -> DispatchReport:
        """Traite un lot de la file. À appeler en boucle par le worker d'envoi.

        Trois phases, délibérément séparées :
          1. réclamer sous transaction courte, puis **valider** ;
          2. résoudre et envoyer, hors transaction ;
          3. enregistrer les résultats sous une nouvelle transaction.

        Fusionner 1 et 2 immobiliserait une connexion PostgreSQL pendant un
        appel chez un fournisseur tiers.
        """
        report = DispatchReport()

        async with self._database.transaction() as connection:
            entries = await self._repository.claim(
                connection, batch_size=self._batch_size, lease_seconds=self._lease_s
            )
        report.claimed = len(entries)

        for entry in entries:
            await self._handle(entry, report)

        if report.claimed:
            logger.info("Envoi des notifications — %s", report.summary())
        return report

    async def _handle(self, entry: OutboxEntry, report: DispatchReport) -> None:
        try:
            recipients = await self._resolve(entry)
        except McpCallFailed as failure:
            await self._after_resolution_failure(entry, failure, report)
            return

        if not recipients:
            # Rôle vacant ou chauffeur introuvable : rien à réessayer, c'est une
            # donnée manquante dans le TMS. Signalé pour reprise humaine.
            reason = f"aucun destinataire pour {entry.target_label} dans le TMS"
            report.no_recipient.append(f"{entry.alert_id}:{entry.target_label}")
            await self._close(entry, NotificationStatus.FAILED, reason)
            report.failed += 1
            logger.warning("Alerte %s : %s", entry.alert_id, reason)
            return

        sent_at = datetime.now(UTC)
        results = [await self._send_to(entry, recipient, sent_at) for recipient in recipients]

        async with self._database.transaction() as connection:
            await self._repository.record_sends(connection, results)
            delivered = sum(1 for item in results if item.status is NotificationStatus.DELIVERED)
            failed = len(results) - delivered
            report.delivered += delivered
            report.failed += failed
            # La demande est close dès qu'elle a été traitée. Un destinataire
            # injoignable est un fait enregistré, pas une raison de tout rejouer :
            # réessayer n'inventerait pas le numéro manquant.
            await self._repository.mark_dispatched(
                connection,
                entry.id,
                status=NotificationStatus.DELIVERED if delivered else NotificationStatus.FAILED,
                failure_reason=None if delivered else "aucun destinataire joint",
            )

    async def _resolve(self, entry: OutboxEntry) -> list[ResolvedRecipient]:
        # La provenance est transmise à chaque résolution : l'annuaire est celui
        # du TMS d'origine, et un superviseur n'est pas interchangeable d'un
        # client à l'autre.
        if entry.user_id:
            # Désignation nominative : la règle a visé une personne précise.
            return await self._mcp.resolve_recipients(
                channel=entry.channel, user_id=entry.user_id, tms_id=entry.tms_id
            )
        return await self._mcp.resolve_recipients(
            channel=entry.channel,
            role=entry.role,
            mission_id=entry.mission_id,
            tms_id=entry.tms_id,
        )

    async def _send_to(
        self,
        entry: OutboxEntry,
        recipient: ResolvedRecipient,
        sent_at: datetime,
    ) -> SentNotification:
        if not recipient.reachable:
            # Échec définitif : une correction à faire dans le TMS, pas un
            # incident passager. Réessayer n'y changerait rien (F17).
            return self._failure(
                entry, recipient, sent_at, recipient.unreachable_reason or "destinataire injoignable"
            )

        channel = self._channels.get(entry.channel)
        if channel is None:
            return self._failure(entry, recipient, sent_at, f"canal {entry.channel.value} non configuré")

        message = Message(
            channel=entry.channel,
            address=recipient.address,
            title=entry.title,
            body=entry.message,
            severity=entry.severity,
            alert_id=str(entry.alert_id),
            mission_id=entry.mission_id,
            recipient_id=recipient.user_id,
            metadata={
                "management_level": recipient.management_level,
                "role": entry.role,
                "tms_id": entry.tms_id,
            },
        )

        try:
            result = await channel.send(message, sent_at=sent_at)
        except Exception as exception:
            logger.exception("Canal %s en erreur", entry.channel.value)
            return self._failure(entry, recipient, sent_at, f"erreur du canal : {exception}")

        return SentNotification(
            outbox_id=entry.id,
            alert_id=entry.alert_id,
            channel=entry.channel,
            management_level=recipient.management_level,
            recipient_id=recipient.user_id,
            recipient_name=recipient.name,
            recipient_role=entry.role,
            address=recipient.address,
            status=NotificationStatus.DELIVERED if result.delivered else NotificationStatus.FAILED,
            failure_reason=result.failure_reason,
            provider=result.provider,
            provider_reference=result.reference,
            sent_at=sent_at,
        )

    @staticmethod
    def _failure(
        entry: OutboxEntry,
        recipient: ResolvedRecipient,
        sent_at: datetime,
        reason: str,
    ) -> SentNotification:
        return SentNotification(
            outbox_id=entry.id,
            alert_id=entry.alert_id,
            channel=entry.channel,
            management_level=recipient.management_level,
            recipient_id=recipient.user_id,
            recipient_name=recipient.name,
            recipient_role=entry.role,
            address=recipient.address,
            status=NotificationStatus.FAILED,
            failure_reason=reason,
            provider=None,
            provider_reference=None,
            sent_at=sent_at,
        )

    async def _after_resolution_failure(
        self,
        entry: OutboxEntry,
        failure: McpCallFailed,
        report: DispatchReport,
    ) -> None:
        """Le TMS ou le serveur MCP n'a pas répondu : on reprogramme, on ne conclut pas.

        Marquer la demande en échec ici reviendrait à ne jamais prévenir le
        superviseur à cause d'une coupure réseau de quelques secondes.
        """
        if not failure.retryable or entry.attempts >= self._max_attempts:
            await self._close(entry, NotificationStatus.FAILED, failure.error.message)
            report.abandoned += 1
            logger.error(
                "Notification abandonnée après %d tentative(s) — alerte %s : %s",
                entry.attempts,
                entry.alert_id,
                failure.error.message,
            )
            return

        await self._reschedule(entry, failure.error.message)
        report.rescheduled += 1

    async def _reschedule(self, entry: OutboxEntry, reason: str) -> None:
        # Progression exponentielle plafonnée : si le TMS est en panne, le
        # marteler au même rythme n'y changera rien.
        delay = min(
            self._retry_backoff_s * (2 ** (entry.attempts - 1)),
            self._retry_backoff_max_s,
        )
        async with self._database.transaction() as connection:
            await self._repository.reschedule(
                connection, entry.id, delay=timedelta(seconds=delay), reason=reason
            )

    async def _close(
        self,
        entry: OutboxEntry,
        status: NotificationStatus,
        reason: str | None,
    ) -> None:
        async with self._database.transaction() as connection:
            await self._repository.mark_dispatched(connection, entry.id, status=status, failure_reason=reason)

    async def pending_count(self) -> int:
        async with self._database.connection() as connection:
            return await self._repository.pending_count(connection)
