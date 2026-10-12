"""File d'attente des notifications (*transaction outbox*).

Les lignes sont écrites dans la **même transaction que l'alerte** par
l'`AlertService` : une alerte sans ses notifications, ou l'inverse, sont deux
états incohérents. Elles sont consommées ensuite, de façon asynchrone, pour
qu'un fournisseur lent ne ralentisse jamais le cycle de surveillance (F17).

**Réclamation par bail, et non par verrou tenu.** Une ligne réclamée voit sa
date d'éligibilité repoussée puis la transaction est validée *avant* tout appel
réseau. Garder la transaction ouverte pendant un envoi SMS immobiliserait une
connexion PostgreSQL pour la durée d'un appel chez un tiers.

Le prix de ce choix est une livraison « au moins une fois » : si le processus
meurt entre l'envoi et l'enregistrement, le bail expire et la demande est
reprise. C'est pourquoi `notifications` porte un index unique sur
`(outbox_id, recipient_id)` — enregistrer deux fois le même envoi est sans
effet, et mieux vaut un SMS en double qu'un superviseur jamais prévenu.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import and_, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import alerts, notification_outbox, notifications
from shared.mapped_dto import NotificationChannel, NotificationStatus, Severity

from .models import OutboxEntry, SentNotification


class OutboxRepository:
    async def claim(
        self,
        connection: AsyncConnection,
        *,
        batch_size: int,
        lease_seconds: int,
    ) -> list[OutboxEntry]:
        """Réclame un lot de demandes éligibles et pose un bail dessus.

        `FOR UPDATE ... SKIP LOCKED` : plusieurs expéditeurs peuvent tourner en
        parallèle sans se marcher dessus ni attendre l'un l'autre. `OF` ne
        verrouille que les lignes de la file, pas les alertes jointes — sinon un
        envoi bloquerait la création d'alertes sur la même mission.
        """
        selection = (
            select(
                notification_outbox.c.id,
                notification_outbox.c.alert_id,
                notification_outbox.c.role,
                notification_outbox.c.user_id,
                notification_outbox.c.management_level,
                notification_outbox.c.channel,
                notification_outbox.c.attempts,
                notification_outbox.c.tms_id,
                alerts.c.mission_id,
                alerts.c.severity,
                alerts.c.title,
                alerts.c.message,
            )
            .join(alerts, alerts.c.id == notification_outbox.c.alert_id)
            .where(
                and_(
                    notification_outbox.c.status == NotificationStatus.PENDING.value,
                    notification_outbox.c.available_at <= func.now(),
                )
            )
            .order_by(notification_outbox.c.available_at, notification_outbox.c.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True, of=notification_outbox)
        )
        rows = list((await connection.execute(selection)).mappings())
        if not rows:
            return []

        claimed_ids = [row["id"] for row in rows]
        await connection.execute(
            update(notification_outbox)
            .where(notification_outbox.c.id.in_(claimed_ids))
            .values(
                attempts=notification_outbox.c.attempts + 1,
                available_at=func.now() + text(f"interval '{int(lease_seconds)} seconds'"),
            )
        )

        return [
            OutboxEntry(
                id=row["id"],
                alert_id=row["alert_id"],
                tms_id=row["tms_id"],
                mission_id=row["mission_id"],
                role=row["role"],
                user_id=row["user_id"],
                management_level=row["management_level"],
                channel=NotificationChannel(row["channel"]),
                attempts=row["attempts"] + 1,
                severity=Severity(row["severity"]),
                title=row["title"],
                message=row["message"],
            )
            for row in rows
        ]

    async def record_sends(
        self,
        connection: AsyncConnection,
        sent: Sequence[SentNotification],
    ) -> int:
        """Enregistre les envois effectués, sans jamais en dupliquer un.

        `ON CONFLICT DO NOTHING` sur `(outbox_id, recipient_id)` : conséquence
        directe de la livraison « au moins une fois » décrite en tête de module.
        """
        if not sent:
            return 0
        statement = (
            pg_insert(notifications)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "outbox_id": item.outbox_id,
                        "alert_id": item.alert_id,
                        "channel": item.channel.value,
                        "management_level": item.management_level,
                        "recipient_id": item.recipient_id,
                        "recipient_name": item.recipient_name,
                        "recipient_role": item.recipient_role,
                        "address": item.address,
                        "status": item.status.value,
                        "failure_reason": item.failure_reason,
                        "provider": item.provider,
                        "provider_reference": item.provider_reference,
                        "sent_at": item.sent_at,
                    }
                    for item in sent
                ]
            )
            .on_conflict_do_nothing(index_elements=[notifications.c.outbox_id, notifications.c.recipient_id])
        )
        result = await connection.execute(statement)
        return result.rowcount or 0

    async def mark_dispatched(
        self,
        connection: AsyncConnection,
        outbox_id: uuid.UUID,
        *,
        status: NotificationStatus,
        failure_reason: str | None = None,
    ) -> None:
        """Clôt une demande : elle a été traitée, en succès ou en échec définitif."""
        await connection.execute(
            update(notification_outbox)
            .where(notification_outbox.c.id == outbox_id)
            .values(
                status=status.value,
                failure_reason=failure_reason,
                sent_at=func.now(),
            )
        )

    async def reschedule(
        self,
        connection: AsyncConnection,
        outbox_id: uuid.UUID,
        *,
        delay: timedelta,
        reason: str,
    ) -> None:
        """Repousse une demande après un incident passager.

        La demande reste `pending` : c'est ce qui la distingue d'un échec
        définitif, et ce qui permet au cycle suivant de la reprendre.
        """
        await connection.execute(
            update(notification_outbox)
            .where(notification_outbox.c.id == outbox_id)
            .values(
                available_at=func.now() + text(f"interval '{int(delay.total_seconds())} seconds'"),
                failure_reason=reason,
            )
        )

    async def pending_count(self, connection: AsyncConnection) -> int:
        """Profondeur de la file, à surveiller : elle ne doit pas croître."""
        result = await connection.execute(
            select(func.count())
            .select_from(notification_outbox)
            .where(notification_outbox.c.status == NotificationStatus.PENDING.value)
        )
        return int(result.scalar_one())

    async def notifications_for_alert(
        self,
        connection: AsyncConnection,
        alert_id: uuid.UUID,
    ) -> list[dict[str, object]]:
        """Envois rattachés à une alerte, pour la console et pour l'enquêteur.

        L'enquêteur en a besoin : savoir qui a déjà été prévenu change ses
        recommandations (F21).
        """
        result = await connection.execute(
            select(notifications)
            .where(notifications.c.alert_id == alert_id)
            .order_by(notifications.c.sent_at)
        )
        return [dict(row) for row in result.mappings()]

    async def enqueue_raw(
        self,
        connection: AsyncConnection,
        rows: Sequence[dict[str, object]],
    ) -> None:
        """Insertion directe, réservée aux tests et aux reprises manuelles."""
        if rows:
            await connection.execute(insert(notification_outbox), list(rows))
