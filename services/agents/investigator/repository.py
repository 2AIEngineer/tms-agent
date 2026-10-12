"""Accès PostgreSQL aux enquêtes.

**Il n'y a pas de table de file d'attente.** La file est la table des alertes
filtrée par « aucune enquête rattachée » : une alerte éligible non encore
réclamée *est* une enquête en attente. Une seconde table aurait dû rester
synchronisée avec la première, pour ne rien apporter.

Réclamer une enquête est donc une **insertion** : `INSERT … SELECT … ON CONFLICT
(alert_id) DO NOTHING RETURNING`. L'index unique sur `alert_id` suffit à
garantir qu'une alerte n'est enquêtée qu'une fois, même par plusieurs instances
du service agent en parallèle — sans verrou ni `SKIP LOCKED`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import SCHEMA, alerts, investigations, notifications
from shared.mapped_dto import (
    Alert,
    AlertStatus,
    InvestigationResult,
    InvestigationStatus,
    Severity,
)
from shared.utils.serialization import to_jsonable

# Ordre de traitement : les plus graves d'abord (F19). Exprimé en SQL pour que
# le tri porte sur l'ensemble des candidates, et non sur le lot déjà réduit.
# `a.severity` et non `severity` : la requête de réclamation joint `alerts` et
# `investigations`, qui portent toutes deux cette colonne. Non qualifiée, la
# référence serait ambiguë et PostgreSQL refuserait la requête.
SEVERITY_RANK_SQL = (
    "case a.severity when 'critical' then 3 when 'high' then 2 when 'medium' then 1 else 0 end"
)


@dataclass(frozen=True, slots=True)
class ClaimedInvestigation:
    """Une enquête réclamée par cette instance, donc qui lui appartient."""

    investigation_id: uuid.UUID
    alert: Alert
    attempts: int
    notifications_sent: tuple[str, ...]


class InvestigationRepository:
    async def claim_batch(
        self,
        connection: AsyncConnection,
        *,
        min_severity: Severity,
        limit: int,
    ) -> list[uuid.UUID]:
        """Réclame les alertes à enquêter, les plus graves d'abord (F19).

        Une seule instruction : la sélection des candidates et la prise de
        possession sont indivisibles. Deux instances lancées en même temps ne
        peuvent pas réclamer la même alerte — l'index unique sur `alert_id`
        rejette la seconde insertion sans erreur.

        Écrite en SQL explicite, comme les autres mécanismes d'atomicité du
        projet : l'exprimer en couches d'objets masquerait exactement ce qui
        fait sa correction.
        """
        eligible = sorted(severity.value for severity in Severity if severity >= min_severity)
        statement = text(f"""
            INSERT INTO {SCHEMA}.investigations
                (id, alert_id, tms_id, mission_id, severity, status, claimed_at)
            SELECT gen_random_uuid(), a.id, a.tms_id, a.mission_id, a.severity,
                   :running, now()
            FROM {SCHEMA}.alerts a
            LEFT JOIN {SCHEMA}.investigations i ON i.alert_id = a.id
            WHERE a.status = :open
              AND a.severity = ANY(:eligible)
              AND i.id IS NULL
            ORDER BY {SEVERITY_RANK_SQL} DESC, a.detected_at
            LIMIT :limit
            ON CONFLICT (alert_id) DO NOTHING
            RETURNING id
        """)
        result = await connection.execute(
            statement,
            {
                "running": InvestigationStatus.RUNNING.value,
                "open": AlertStatus.OPEN.value,
                "eligible": eligible,
                "limit": limit,
            },
        )
        return [row.id for row in result]

    async def reclaim_stale(
        self,
        connection: AsyncConnection,
        *,
        older_than: datetime,
        max_attempts: int,
    ) -> list[uuid.UUID]:
        """Reprend les enquêtes abandonnées en cours de route.

        Une instance tuée au milieu d'une enquête laisse une ligne `running`
        que personne ne reprendrait. Sans cette reprise, l'alerte resterait
        sans analyse pour toujours — et comme l'alerte elle-même existe, rien
        ne le signalerait.
        """
        statement = (
            update(investigations)
            .where(
                and_(
                    investigations.c.status == InvestigationStatus.RUNNING.value,
                    investigations.c.claimed_at < older_than,
                    investigations.c.attempts < max_attempts,
                )
            )
            .values(attempts=investigations.c.attempts + 1, claimed_at=func.now())
            .returning(investigations.c.id)
        )
        result = await connection.execute(statement)
        return [row.id for row in result]

    async def reclaim_skipped(
        self,
        connection: AsyncConnection,
        *,
        older_than: datetime,
        max_attempts: int,
    ) -> list[uuid.UUID]:
        """Reprend les enquêtes **sautées** faute de modèle disponible.

        Une enquête sautée n'est pas un échec : le LLM n'était pas là. Sans
        cette reprise, elle le resterait pour toujours — la file est la table
        des alertes filtrée par « aucune enquête rattachée », et une ligne
        `skipped` suffit à écarter l'alerte définitivement.

        Mesuré en conditions réelles : un quota d'inférence épuisé pendant
        quelques minutes privait d'analyse toutes les alertes de cette
        fenêtre, sans que rien ne le signale — l'alerte, elle, existait bien.

        Seules les alertes **encore ouvertes** sont reprises : analyser une
        alerte close depuis une heure n'apprend plus rien à personne.
        """
        statement = (
            update(investigations)
            .where(
                and_(
                    investigations.c.status == InvestigationStatus.SKIPPED.value,
                    investigations.c.completed_at < older_than,
                    investigations.c.attempts < max_attempts,
                    investigations.c.alert_id.in_(
                        select(alerts.c.id).where(alerts.c.status == AlertStatus.OPEN.value)
                    ),
                )
            )
            .values(
                status=InvestigationStatus.RUNNING.value,
                attempts=investigations.c.attempts + 1,
                claimed_at=func.now(),
                error=None,
            )
            .returning(investigations.c.id)
        )
        result = await connection.execute(statement)
        return [row.id for row in result]

    async def load(
        self,
        connection: AsyncConnection,
        investigation_id: uuid.UUID,
    ) -> ClaimedInvestigation | None:
        """Charge le contexte d'une enquête réclamée."""
        # Colonnes nommées explicitement : les deux tables portent une colonne
        # `id` et une colonne `severity`, et une sélection globale rendrait la
        # ligne ambiguë — l'identifiant de l'enquête écrasant celui de l'alerte.
        statement = (
            select(
                investigations.c.id.label("investigation_id"),
                investigations.c.attempts,
                alerts.c.id.label("alert_id"),
                alerts.c.tms_id,
                alerts.c.mission_id,
                alerts.c.rule_id,
                alerts.c.rule_version,
                alerts.c.severity,
                alerts.c.category,
                alerts.c.title,
                alerts.c.message,
                alerts.c.observed_data,
                alerts.c.deduplication_key,
                alerts.c.cooldown_minutes,
                alerts.c.auto_resolve,
                alerts.c.event_id,
                alerts.c.status,
                alerts.c.detected_at,
                alerts.c.resolved_at,
            )
            .join(alerts, alerts.c.id == investigations.c.alert_id)
            .where(investigations.c.id == investigation_id)
        )
        row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None

        sent = await connection.execute(
            select(
                notifications.c.channel,
                notifications.c.recipient_role,
                notifications.c.management_level,
                notifications.c.status,
            ).where(notifications.c.alert_id == row["alert_id"])
        )
        return ClaimedInvestigation(
            investigation_id=row["investigation_id"],
            alert=Alert(
                id=row["alert_id"],
                tms_id=row["tms_id"],
                mission_id=row["mission_id"],
                rule_id=row["rule_id"],
                rule_version=row["rule_version"],
                severity=Severity(row["severity"]),
                category=row["category"],
                title=row["title"],
                message=row["message"],
                observed_data=row["observed_data"] or {},
                deduplication_key=row["deduplication_key"],
                cooldown_minutes=row["cooldown_minutes"],
                auto_resolve=row["auto_resolve"],
                event_id=row["event_id"],
                status=AlertStatus(row["status"]),
                detected_at=row["detected_at"],
                resolved_at=row["resolved_at"],
            ),
            attempts=row["attempts"],
            notifications_sent=tuple(
                f"{item.recipient_role or '?'} ({item.management_level}) par {item.channel} : {item.status}"
                for item in sent
            ),
        )

    async def record_facts(
        self,
        connection: AsyncConnection,
        investigation_id: uuid.UUID,
        facts: dict[str, Any],
    ) -> None:
        """Conserve les faits déterministes, pour pouvoir rejouer l'enquête."""
        await connection.execute(
            update(investigations)
            .where(investigations.c.id == investigation_id)
            .values(observed_facts=to_jsonable(facts))
        )

    async def complete(
        self,
        connection: AsyncConnection,
        investigation_id: uuid.UUID,
        result: InvestigationResult,
        *,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await connection.execute(
            update(investigations)
            .where(investigations.c.id == investigation_id)
            .values(
                status=InvestigationStatus.COMPLETED.value,
                result=to_jsonable(result),
                error=None,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                completed_at=func.now(),
            )
        )

    async def fail(
        self,
        connection: AsyncConnection,
        investigation_id: uuid.UUID,
        reason: str,
        *,
        status: InvestigationStatus = InvestigationStatus.FAILED,
    ) -> None:
        """Enregistre un échec d'enquête.

        L'alerte n'est pas touchée : elle reste valable et notifiée, seule
        l'analyse manque (F19, résilience LLM). `skipped` distingue une
        indisponibilité du LLM d'une vraie erreur d'enquête.
        """
        await connection.execute(
            update(investigations)
            .where(investigations.c.id == investigation_id)
            .values(status=status.value, error=reason[:2000], completed_at=func.now())
        )

    async def requeue(self, connection: AsyncConnection, investigation_id: uuid.UUID) -> None:
        """Remet une enquête en attente, pour une nouvelle tentative."""
        await connection.execute(
            update(investigations)
            .where(investigations.c.id == investigation_id)
            .values(
                status=InvestigationStatus.QUEUED.value,
                attempts=investigations.c.attempts + 1,
                claimed_at=None,
            )
        )

    async def pending_count(self, connection: AsyncConnection) -> int:
        """Alertes éligibles sans enquête, plus enquêtes en cours.

        Compteur à surveiller : s'il croît, l'enquête ne tient pas le rythme de
        la détection.
        """
        statement = (
            select(func.count())
            .select_from(investigations)
            .where(
                or_(
                    investigations.c.status == InvestigationStatus.QUEUED.value,
                    investigations.c.status == InvestigationStatus.RUNNING.value,
                )
            )
        )
        return int((await connection.execute(statement)).scalar_one())

    async def result_for_alert(
        self,
        connection: AsyncConnection,
        alert_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Évaluation jointe à une alerte, pour l'interface et les rapports."""
        statement = select(investigations.c.status, investigations.c.result).where(
            investigations.c.alert_id == alert_id
        )
        row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        return {"status": row["status"], "result": row["result"]}

    async def claim_specific(
        self,
        connection: AsyncConnection,
        alert_ids: Sequence[uuid.UUID],
    ) -> list[uuid.UUID]:
        """Réclame des alertes nommées. Réservé aux tests et aux reprises manuelles."""
        if not alert_ids:
            return []
        rows = (
            await connection.execute(
                select(alerts.c.id, alerts.c.tms_id, alerts.c.mission_id, alerts.c.severity).where(
                    alerts.c.id.in_(alert_ids)
                )
            )
        ).mappings()
        statement = (
            pg_insert(investigations)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "alert_id": row["id"],
                        "tms_id": row["tms_id"],
                        "mission_id": row["mission_id"],
                        "severity": row["severity"],
                        "status": InvestigationStatus.RUNNING.value,
                        "claimed_at": func.now(),
                    }
                    for row in rows
                ]
            )
            .on_conflict_do_nothing(index_elements=[investigations.c.alert_id])
            .returning(investigations.c.id)
        )
        return [row.id for row in await connection.execute(statement)]
