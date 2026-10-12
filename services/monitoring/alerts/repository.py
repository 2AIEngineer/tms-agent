"""Accès PostgreSQL aux alertes, à la file d'envoi et au journal d'audit.

Toutes les requêtes prennent la connexion en paramètre au lieu de l'ouvrir :
c'est l'appelant qui décide du périmètre de la transaction. Sans cela,
l'atomicité promise par le service — alerte + notifications + marquage de
l'événement écrits ensemble — serait impossible à obtenir.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import alerts, audit_log, notification_outbox, processed_events
from shared.mapped_dto import (
    Alert,
    AlertStatus,
    MissionEvent,
    NotificationRequest,
    ResolutionReason,
    Severity,
)
from shared.utils.serialization import to_jsonable


@dataclass(frozen=True, slots=True)
class OpenAlert:
    """Vue minimale d'une alerte ouverte, suffisante pour décider de sa clôture."""

    id: uuid.UUID
    tms_id: str
    rule_id: str
    rule_version: int
    severity: Severity
    deduplication_key: str
    auto_resolve: bool
    event_id: str | None
    detected_at: datetime

    @property
    def is_event_alert(self) -> bool:
        return self.event_id is not None


class AlertRepository:
    """Écritures et lectures des tables du cycle de vie des alertes."""

    # --- Création ------------------------------------------------------------

    async def insert_alert(self, connection: AsyncConnection, alert: Alert) -> uuid.UUID | None:
        """Insère l'alerte, ou rend `None` si une alerte ouverte occupe déjà la clé.

        `ON CONFLICT` sur l'index unique **partiel** des alertes ouvertes : la
        déduplication est tranchée par la base, en une seule instruction. Un
        « vérifier puis insérer » côté application laisserait une fenêtre entre
        les deux, et deux alertes identiques pourraient naître.
        """
        statement = (
            pg_insert(alerts)
            .values(
                id=alert.id,
                tms_id=alert.tms_id,
                mission_id=alert.mission_id,
                rule_id=alert.rule_id,
                rule_version=alert.rule_version,
                severity=alert.severity.value,
                category=alert.category,
                title=alert.title,
                message=alert.message,
                observed_data=to_jsonable(alert.observed_data),
                deduplication_key=alert.deduplication_key,
                cooldown_minutes=alert.cooldown_minutes,
                auto_resolve=alert.auto_resolve,
                event_id=alert.event_id,
                status=alert.status.value,
                detected_at=alert.detected_at,
            )
            .on_conflict_do_nothing(
                index_elements=[alerts.c.tms_id, alerts.c.deduplication_key],
                # Prédicat écrit **littéralement**, et non comparé à une valeur
                # liée. PostgreSQL doit apparier ce `WHERE` au prédicat de
                # l'index partiel au moment de la planification. Avec un
                # paramètre lié, l'appariement réussit tant que le serveur
                # utilise un plan personnalisé, puis échoue dès qu'il bascule
                # sur un plan générique — après cinq exécutions de la même
                # requête préparée. L'insertion se mettrait alors à lever
                # « no unique or exclusion constraint matching the ON CONFLICT
                # specification » en exploitation, après quelques alertes.
                index_where=text(f"status = '{AlertStatus.OPEN.value}'"),
            )
            .returning(alerts.c.id)
        )
        result = await connection.execute(statement)
        inserted: uuid.UUID | None = result.scalar_one_or_none()
        return inserted

    async def has_open_alert(
        self,
        connection: AsyncConnection,
        tms_id: str,
        deduplication_key: str,
    ) -> bool:
        """Une alerte ouverte occupe-t-elle déjà cette clé ?

        Interrogé avant le cooldown pour que la raison de suppression soit la
        bonne : une alerte encore ouverte est un doublon, pas un cooldown. La
        distinction compte, c'est elle qu'un exploitant lira dans le bilan de
        cycle pour comprendre pourquoi il n'a rien reçu.
        """
        statement = (
            select(alerts.c.id)
            .where(
                and_(
                    alerts.c.tms_id == tms_id,
                    alerts.c.deduplication_key == deduplication_key,
                    alerts.c.status == AlertStatus.OPEN.value,
                )
            )
            .limit(1)
        )
        result = await connection.execute(statement)
        return result.first() is not None

    async def last_detected_at(
        self,
        connection: AsyncConnection,
        tms_id: str,
        deduplication_key: str,
    ) -> datetime | None:
        """Heure TMS du dernier passage de cette clé, alertes closes comprises.

        Les alertes closes comptent : le cooldown sert précisément à empêcher
        qu'un problème résolu puis réapparu renotifie dans la minute.
        """
        statement = (
            select(alerts.c.detected_at)
            .where(
                and_(
                    alerts.c.tms_id == tms_id,
                    alerts.c.deduplication_key == deduplication_key,
                )
            )
            .order_by(alerts.c.detected_at.desc())
            .limit(1)
        )
        result = await connection.execute(statement)
        previous: datetime | None = result.scalar_one_or_none()
        return previous

    async def is_within_cooldown(
        self,
        connection: AsyncConnection,
        tms_id: str,
        deduplication_key: str,
        cooldown_minutes: int,
        tms_now: datetime,
    ) -> bool:
        """Le cooldown est-il encore en cours, en heure TMS ?

        À appeler sous verrou consultatif sur la même clé, sinon le contrôle
        redevient un « lire puis écrire » non atomique.
        """
        if cooldown_minutes <= 0:
            return False
        previous = await self.last_detected_at(connection, tms_id, deduplication_key)
        if previous is None:
            return False
        return previous > tms_now - timedelta(minutes=cooldown_minutes)

    # --- Clôture -------------------------------------------------------------

    async def open_alerts(
        self,
        connection: AsyncConnection,
        tms_id: str,
        mission_id: str,
        *,
        rule_id: str | None = None,
        state_only: bool = False,
    ) -> list[OpenAlert]:
        conditions = [
            alerts.c.tms_id == tms_id,
            alerts.c.mission_id == mission_id,
            alerts.c.status == AlertStatus.OPEN.value,
        ]
        if rule_id is not None:
            conditions.append(alerts.c.rule_id == rule_id)
        if state_only:
            conditions.append(alerts.c.event_id.is_(None))

        statement = (
            select(
                alerts.c.id,
                alerts.c.tms_id,
                alerts.c.rule_id,
                alerts.c.rule_version,
                alerts.c.severity,
                alerts.c.deduplication_key,
                alerts.c.auto_resolve,
                alerts.c.event_id,
                alerts.c.detected_at,
            )
            .where(and_(*conditions))
            .order_by(alerts.c.detected_at)
        )
        result = await connection.execute(statement)
        return [
            OpenAlert(
                id=row.id,
                tms_id=row.tms_id,
                rule_id=row.rule_id,
                rule_version=row.rule_version,
                severity=Severity(row.severity),
                deduplication_key=row.deduplication_key,
                auto_resolve=row.auto_resolve,
                event_id=row.event_id,
                detected_at=row.detected_at,
            )
            for row in result
        ]

    async def resolve(
        self,
        connection: AsyncConnection,
        alert_ids: Sequence[uuid.UUID],
        *,
        resolved_at: datetime,
        reason: ResolutionReason,
    ) -> int:
        """Clôt des alertes. Le filtre sur `status` rend l'opération idempotente."""
        if not alert_ids:
            return 0
        statement = (
            update(alerts)
            .where(and_(alerts.c.id.in_(alert_ids), alerts.c.status == AlertStatus.OPEN.value))
            .values(
                status=AlertStatus.RESOLVED.value,
                resolved_at=resolved_at,
                resolution_reason=reason.value,
                updated_at=func.now(),
            )
        )
        result = await connection.execute(statement)
        return result.rowcount or 0

    # --- File d'envoi des notifications --------------------------------------

    async def enqueue_notifications(
        self,
        connection: AsyncConnection,
        alert_id: uuid.UUID,
        requests: Sequence[NotificationRequest],
        *,
        tms_id: str,
    ) -> int:
        """Écrit les envois à faire dans la table d'attente (*outbox*).

        Dans la **même transaction** que l'alerte : une alerte sans ses
        notifications, ou des notifications sans leur alerte, sont deux états
        incohérents. L'envoi effectif est asynchrone, pour qu'un fournisseur
        lent ne ralentisse jamais le cycle de surveillance (F17).
        """
        if not requests:
            return 0
        await connection.execute(
            insert(notification_outbox),
            [
                {
                    "id": uuid.uuid4(),
                    "alert_id": alert_id,
                    "tms_id": tms_id,
                    "role": request.role,
                    "user_id": request.user_id,
                    "management_level": request.management_level,
                    "channel": request.channel.value,
                }
                for request in requests
            ],
        )
        return len(requests)

    # --- Idempotence des événements ------------------------------------------

    async def unprocessed_event_ids(
        self,
        connection: AsyncConnection,
        tms_id: str,
        event_ids: Sequence[str],
    ) -> set[str]:
        """Parmi ces identifiants, ceux que **ce TMS** n'a jamais vus traités (F7).

        Le filtre sur la provenance est indispensable : deux TMS peuvent
        numéroter leurs événements de la même façon, et sans lui l'événement du
        second passerait pour déjà traité — son alerte ne partirait jamais.
        """
        if not event_ids:
            return set()
        statement = select(processed_events.c.event_id).where(
            and_(
                processed_events.c.tms_id == tms_id,
                processed_events.c.event_id.in_(event_ids),
            )
        )
        result = await connection.execute(statement)
        already = {row.event_id for row in result}
        return {event_id for event_id in event_ids if event_id not in already}

    async def mark_event_processed(
        self,
        connection: AsyncConnection,
        event: MissionEvent,
        *,
        tms_id: str,
        processed_at: datetime,
        alerts_created: int,
    ) -> None:
        """Marque l'événement traité, dans la transaction de ses alertes.

        `ON CONFLICT DO NOTHING` : un même événement peut être présenté deux
        fois si un cycle a échoué après la transaction mais avant son bilan.
        """
        statement = (
            pg_insert(processed_events)
            .values(
                tms_id=tms_id,
                event_id=event.id,
                mission_id=event.mission_id,
                event_type=event.type.value,
                occurred_at=event.occurred_at,
                processed_at=processed_at,
                alerts_created=alerts_created,
            )
            .on_conflict_do_nothing(index_elements=[processed_events.c.tms_id, processed_events.c.event_id])
        )
        await connection.execute(statement)

    async def forget_events_before(self, connection: AsyncConnection, cutoff: datetime) -> int:
        """Purge les événements traités antérieurs à `cutoff`.

        La table ne sert qu'à l'idempotence : la garder entière la ferait
        croître indéfiniment, alors qu'un événement vieux de plusieurs jours ne
        sera plus jamais présenté par le TMS.
        """
        result = await connection.execute(
            delete(processed_events).where(processed_events.c.occurred_at < cutoff)
        )
        return result.rowcount or 0

    # --- Journal d'audit ------------------------------------------------------

    async def append_audit(
        self,
        connection: AsyncConnection,
        entries: Sequence[dict[str, object]],
    ) -> None:
        """Ajoute des entrées au journal. En ajout seul, jamais modifiées (F27).

        Les entrées sont insérées par groupes de **même forme**, et c'est
        indispensable : une insertion multi-lignes exige que toutes les lignes
        portent les mêmes clés. Une entrée `alert_created` (qui porte un
        `alert_id`) voisine d'une entrée `alert_suppressed` (qui n'en a pas)
        faisait échouer l'insertion entière.

        Mesuré en conditions réelles, sur un cycle où une règle déclenchait et
        une autre était supprimée pour doublon sur la **même mission** : le
        journal étant écrit dans la transaction de l'alerte, c'est l'alerte
        elle-même qui était perdue, et le cycle rapportait seulement « mission
        illisible ». Une alerte d'immobilisation est restée invisible une heure
        durant pour cette raison.

        Les clés absentes ne peuvent pas être comblées par `None` : `actor` et
        `detail` sont non nuls et tiennent leur valeur d'un défaut côté base.
        Le regroupement porte donc sur des entrées **consécutives**, ce qui
        préserve l'ordre d'écriture tout en n'émettant qu'une instruction dans
        le cas courant, où toutes les entrées ont la même forme.
        """
        if not entries:
            return
        batch: list[dict[str, object]] = []
        signature: tuple[str, ...] | None = None
        for entry in entries:
            row = {**entry, "detail": to_jsonable(entry.get("detail", {}))}
            shape = tuple(sorted(row))
            if signature is not None and shape != signature:
                await connection.execute(insert(audit_log), batch)
                batch = []
            signature = shape
            batch.append(row)
        if batch:
            await connection.execute(insert(audit_log), batch)
