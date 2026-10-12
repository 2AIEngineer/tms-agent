"""Lectures de l'API d'exploitation.

**Lecture seule, sans exception.** Les écritures de l'API passent par les
services qui en sont propriétaires : l'assistant pour les actions d'opérateur,
le worker de surveillance pour les alertes. Une requête `UPDATE` ici
contournerait les garde-fous et le journal d'audit qui vont avec.

Les requêtes rendent des dictionnaires et non des contrats pivot : ce sont des
vues d'affichage, assemblées pour une interface, et les forcer dans des modèles
d'échange entre services ferait porter à ces contrats des préoccupations de
présentation.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Case, ColumnElement, Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import alerts, audit_log, driver_calls, investigations, notifications
from services.monitoring.state import MonitoringStateRepository
from shared.mapped_dto import AlertStatus, Severity

# Plafond de pagination. Une interface qui demande mille alertes ne les affiche
# pas : elle fige l'onglet de l'opérateur et charge la base pour rien.
MAX_PAGE_SIZE = 200


class OpsRepository:
    """Vues de lecture pour l'interface d'exploitation."""

    async def list_alerts(
        self,
        connection: AsyncConnection,
        *,
        tms_ids: Sequence[str] = (),
        status: AlertStatus | None = None,
        min_severity: Severity | None = None,
        mission_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Alertes filtrées, les plus graves et les plus récentes d'abord.

        L'ordre est celui dont un agent de suivi a besoin : il traite les
        critiques avant les moyennes, et dans chaque niveau les plus fraîches.

        **`tms_ids` vide signifie « aucune restriction »**, pas « aucun TMS » :
        la convention est piégeuse et l'appelant doit s'arrêter de lui-même
        lorsqu'un périmètre se réduit à l'ensemble vide.
        """
        query = select(
            alerts.c.id,
            alerts.c.tms_id,
            alerts.c.mission_id,
            alerts.c.rule_id,
            alerts.c.rule_version,
            alerts.c.severity,
            alerts.c.category,
            alerts.c.title,
            alerts.c.message,
            alerts.c.status,
            alerts.c.detected_at,
            alerts.c.resolved_at,
            alerts.c.resolution_reason,
            alerts.c.observed_data,
        )
        query = self._filter_alerts(
            query,
            tms_ids=tms_ids,
            status=status,
            min_severity=min_severity,
            mission_id=mission_id,
            since=since,
        )
        ordered = (
            query.order_by(_severity_rank().desc(), alerts.c.detected_at.desc())
            .limit(min(limit, MAX_PAGE_SIZE))
            .offset(offset)
        )
        result = await connection.execute(ordered)
        return [dict(row) for row in result.mappings()]

    async def count_alerts(
        self,
        connection: AsyncConnection,
        *,
        tms_ids: Sequence[str] = (),
        status: AlertStatus | None = None,
        min_severity: Severity | None = None,
        mission_id: str | None = None,
        since: datetime | None = None,
    ) -> int:
        query = self._filter_alerts(
            select(func.count()).select_from(alerts),
            tms_ids=tms_ids,
            status=status,
            min_severity=min_severity,
            mission_id=mission_id,
            since=since,
        )
        return int((await connection.execute(query)).scalar_one())

    async def alert_detail(
        self,
        connection: AsyncConnection,
        alert_id: uuid.UUID,
        *,
        tms_ids: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        """Une alerte avec tout ce qui s'y rattache.

        Rassemblé en une réponse plutôt qu'en quatre appels : l'interface
        affiche l'alerte, son analyse, ses notifications et ses appels sur le
        même écran, et quatre allers-retours en rendraient l'ouverture lente
        pour rien.
        """
        query = select(alerts).where(alerts.c.id == alert_id)
        if tms_ids:
            query = query.where(alerts.c.tms_id.in_(tuple(tms_ids)))
        row = (await connection.execute(query)).mappings().first()
        if row is None:
            return None

        detail = dict(row)
        detail["investigation"] = await self._investigation_of(connection, alert_id)
        detail["notifications"] = await self._notifications_of(connection, alert_id)
        detail["driver_calls"] = await self._calls_of(connection, alert_id)
        detail["audit"] = await self._audit_of(connection, alert_id)
        return detail

    async def new_alerts_since(
        self,
        connection: AsyncConnection,
        *,
        after: datetime,
        tms_ids: Sequence[str] = (),
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Alertes enregistrées après un instant donné, pour le flux temps réel.

        Le curseur porte sur `created_at`, l'horloge du **serveur**, et non sur
        `detected_at`, qui est l'heure du TMS : en simulation accélérée l'heure
        TMS avance plus vite que le temps réel, et un curseur posé dessus
        sauterait des alertes.
        """
        query = select(
            alerts.c.id,
            alerts.c.tms_id,
            alerts.c.mission_id,
            alerts.c.rule_id,
            alerts.c.severity,
            alerts.c.category,
            alerts.c.title,
            alerts.c.message,
            alerts.c.status,
            alerts.c.detected_at,
            alerts.c.created_at,
        ).where(alerts.c.created_at > after)
        if tms_ids:
            query = query.where(alerts.c.tms_id.in_(tuple(tms_ids)))
        query = query.order_by(alerts.c.created_at).limit(limit)
        result = await connection.execute(query)
        return [dict(row) for row in result.mappings()]

    async def summary(
        self,
        connection: AsyncConnection,
        *,
        tms_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Compte des alertes ouvertes par TMS et par sévérité.

        Ce que le bandeau d'une interface d'exploitation affiche en permanence,
        et ce qu'un tableau mural montre : une requête agrégée plutôt qu'un
        comptage côté client sur une liste paginée.
        """
        query = (
            select(alerts.c.tms_id, alerts.c.severity, func.count().label("count"))
            .where(alerts.c.status == AlertStatus.OPEN.value)
            .group_by(alerts.c.tms_id, alerts.c.severity)
        )
        if tms_ids:
            query = query.where(alerts.c.tms_id.in_(tuple(tms_ids)))
        rows = list((await connection.execute(query)).mappings())

        by_tms: dict[str, dict[str, int]] = {}
        by_severity: dict[str, int] = {}
        for row in rows:
            by_tms.setdefault(row["tms_id"], {})[row["severity"]] = row["count"]
            by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + row["count"]
        return {
            "open_alerts": sum(by_severity.values()),
            "by_severity": by_severity,
            "by_tms": by_tms,
        }

    async def monitoring_health(
        self,
        connection: AsyncConnection,
        *,
        tms_ids: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Dernier battement de cœur par TMS (F27).

        « Le worker tourne mais n'arrive pas à lire le TMS » et « le worker ne
        tourne plus » n'appellent pas la même intervention : les deux doivent
        être lisibles dans l'interface.
        """
        states = await MonitoringStateRepository().all_cycles(connection)
        if tms_ids:
            states = [state for state in states if state.get("tms_id") in tuple(tms_ids)]
        return states

    async def investigation_queue(self, connection: AsyncConnection) -> dict[str, int]:
        """Taille de la file d'enquêtes par état, pour le diagnostic."""
        result = await connection.execute(
            select(investigations.c.status, func.count().label("count")).group_by(investigations.c.status)
        )
        return {row["status"]: row["count"] for row in result.mappings()}

    # --- Sous-lectures d'une alerte -------------------------------------------

    @staticmethod
    async def _investigation_of(
        connection: AsyncConnection,
        alert_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        result = await connection.execute(
            select(
                investigations.c.id,
                investigations.c.status,
                investigations.c.result,
                investigations.c.error,
                investigations.c.model,
                investigations.c.input_tokens,
                investigations.c.output_tokens,
                investigations.c.duration_ms,
                investigations.c.completed_at,
            ).where(investigations.c.alert_id == alert_id)
        )
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    async def _notifications_of(
        connection: AsyncConnection,
        alert_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        result = await connection.execute(
            select(
                notifications.c.channel,
                notifications.c.recipient_name,
                notifications.c.recipient_role,
                notifications.c.management_level,
                notifications.c.status,
                notifications.c.failure_reason,
                notifications.c.sent_at,
            )
            .where(notifications.c.alert_id == alert_id)
            .order_by(notifications.c.sent_at)
        )
        return [dict(row) for row in result.mappings()]

    @staticmethod
    async def _calls_of(connection: AsyncConnection, alert_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await connection.execute(
            select(
                driver_calls.c.status,
                driver_calls.c.blocked_reason,
                driver_calls.c.requested_by,
                driver_calls.c.reason,
                driver_calls.c.requested_at,
            )
            .where(driver_calls.c.alert_id == alert_id)
            .order_by(driver_calls.c.requested_at)
        )
        return [dict(row) for row in result.mappings()]

    @staticmethod
    async def _audit_of(connection: AsyncConnection, alert_id: uuid.UUID) -> list[dict[str, Any]]:
        result = await connection.execute(
            select(
                audit_log.c.action,
                audit_log.c.actor,
                audit_log.c.detail,
                audit_log.c.occurred_at,
            )
            .where(audit_log.c.alert_id == alert_id)
            .order_by(audit_log.c.occurred_at)
        )
        return [dict(row) for row in result.mappings()]

    # --- Filtres partagés -----------------------------------------------------

    @staticmethod
    def _filter_alerts(
        query: Select[Any],
        *,
        tms_ids: Sequence[str],
        status: AlertStatus | None,
        min_severity: Severity | None,
        mission_id: str | None,
        since: datetime | None,
    ) -> Select[Any]:
        """Applique les mêmes filtres à la liste et à son comptage.

        Partagés délibérément : un filtre appliqué à la liste mais oublié dans
        le comptage produirait une pagination qui ne tombe jamais juste, et le
        défaut passerait longtemps inaperçu.
        """
        conditions: list[ColumnElement[bool]] = []
        if tms_ids:
            conditions.append(alerts.c.tms_id.in_(tuple(tms_ids)))
        if status is not None:
            conditions.append(alerts.c.status == status.value)
        if min_severity is not None:
            eligible = [item.value for item in Severity if item.rank >= min_severity.rank]
            conditions.append(alerts.c.severity.in_(eligible))
        if mission_id:
            conditions.append(alerts.c.mission_id == mission_id)
        if since is not None:
            conditions.append(alerts.c.detected_at >= since)
        return query.where(and_(*conditions)) if conditions else query


def _severity_rank() -> Case[int]:
    """Ordre métier des sévérités, en SQL.

    `order by severity` trierait par ordre alphabétique — `critical` avant
    `low`, et `high` avant `medium`. L'ordre doit être celui de la gravité.
    """
    return case(
        {item.value: item.rank for item in Severity},
        value=alerts.c.severity,
        else_=0,
    )
