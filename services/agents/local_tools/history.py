"""Historique de l'orchestrateur sur une mission (F21).

La spécification accorde à l'enquêteur le droit de lire « l'historique de
l'agent ». Cela ne passe pas par MCP : cet historique est **le nôtre** — nos
alertes, nos enquêtes, nos appels — et le serveur MCP est la frontière du TMS,
pas celle de notre base. C'est donc un outil **local** du service agent, comme
`call_driver`.

Ce qu'il apporte, et qu'aucune lecture du TMS ne donne : la **récurrence**. Une
dérive de 7 km est anodine si cette mission dérive à cet endroit tous les
jours, et préoccupante si c'est la première fois. Un LLM ne peut pas le savoir
sans le lui dire, et il ne doit pas le supposer.
"""

import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import alerts, audit_log, driver_calls, investigations
from shared.mapped_dto import InvestigationRequest
from shared.utils.db import Database
from shared.utils.serialization import to_jsonable

logger = logging.getLogger(__name__)

HISTORY_TOOL_NAME = "get_agent_history"

# Plafonds de lecture. L'historique part dans la fenêtre de contexte : au-delà,
# il chasse les faits déterministes sans rien apprendre de plus.
MAX_ALERTS = 10
MAX_CALLS = 5
MAX_DECISIONS = 5


class HistoryArguments(BaseModel):
    """Aucun identifiant : la mission est celle de l'enquête en cours."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        default=MAX_ALERTS,
        ge=1,
        le=MAX_ALERTS,
        description="Nombre d'alertes passées à rendre, de la plus récente à la plus ancienne.",
    )


def history_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": HISTORY_TOOL_NAME,
            "description": (
                "Historique de l'orchestrateur sur cette mission : alertes passées "
                "et leur clôture, enquêtes précédentes, appels au chauffeur, "
                "décisions d'opérateur. À lire pour juger si la situation est "
                "récurrente ou nouvelle."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Alertes passées à rendre (1 à 10).",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


class AgentHistoryTool:
    """Rend à l'enquêteur ce que l'orchestrateur sait déjà de cette mission."""

    name = HISTORY_TOOL_NAME

    def __init__(self, database: Database) -> None:
        self._database = database

    @staticmethod
    def definition() -> dict[str, Any]:
        return history_tool_definition()

    async def invoke(self, request: InvestigationRequest, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = HistoryArguments.model_validate(arguments or {})
        except ValidationError as error:
            return {"ok": False, "error": {"message": f"Argument invalide : {error.errors()[0]['msg']}"}}

        async with self._database.connection() as connection:
            history = await read_history(
                connection,
                tms_id=request.tms_id,
                mission_id=request.mission_id,
                current_alert_id=request.alert_id,
                limit=parsed.limit,
            )
        return {"ok": True, **to_jsonable(history)}


async def read_history(
    connection: AsyncConnection,
    *,
    tms_id: str,
    mission_id: str,
    current_alert_id: uuid.UUID | None = None,
    limit: int = MAX_ALERTS,
) -> dict[str, Any]:
    """Assemble l'historique d'une mission, tel qu'il sera rendu au modèle.

    Fonction libre et non méthode : l'assistant opérateur a le même besoin, et
    la logique de lecture n'a pas à être dupliquée pour deux appelants.
    """
    past_alerts = await _alerts_of(connection, tms_id, mission_id, limit)
    return {
        "tms_id": tms_id,
        "mission_id": mission_id,
        "alert_count": len(past_alerts),
        "alerts": [{**alert, "is_current": alert["id"] == current_alert_id} for alert in past_alerts],
        # Comptage par règle : c'est lui qui dit « cette mission a déjà dérivé
        # trois fois », et le modèle n'a pas à compter lui-même.
        "alerts_per_rule": _count_by(past_alerts, "rule_id"),
        "investigations": await _investigations_of(connection, tms_id, mission_id),
        "driver_calls": await _calls_of(connection, tms_id, mission_id),
        "operator_decisions": await _decisions_of(connection, tms_id, mission_id),
    }


async def _alerts_of(
    connection: AsyncConnection,
    tms_id: str,
    mission_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    result = await connection.execute(
        select(
            alerts.c.id,
            alerts.c.rule_id,
            alerts.c.severity,
            alerts.c.category,
            alerts.c.status,
            alerts.c.detected_at,
            alerts.c.resolved_at,
            alerts.c.resolution_reason,
        )
        .where(and_(alerts.c.tms_id == tms_id, alerts.c.mission_id == mission_id))
        .order_by(alerts.c.detected_at.desc())
        .limit(limit)
    )
    return [dict(row) for row in result.mappings()]


async def _investigations_of(
    connection: AsyncConnection,
    tms_id: str,
    mission_id: str,
) -> list[dict[str, Any]]:
    """Enquêtes précédentes, réduites à leur conclusion.

    Le résultat complet d'une enquête pèse plus que tout le reste de
    l'historique réuni. Seules la synthèse, la cause probable et le soupçon de
    fausse alerte servent à juger de la récurrence.
    """
    result = await connection.execute(
        select(
            investigations.c.status,
            investigations.c.result,
            investigations.c.completed_at,
        )
        .where(and_(investigations.c.tms_id == tms_id, investigations.c.mission_id == mission_id))
        .order_by(investigations.c.created_at.desc())
        .limit(MAX_CALLS)
    )
    summaries: list[dict[str, Any]] = []
    for row in result.mappings():
        outcome = row["result"] if isinstance(row["result"], dict) else {}
        summaries.append(
            {
                "status": row["status"],
                "completed_at": row["completed_at"],
                "summary": outcome.get("summary"),
                "probable_cause": outcome.get("probable_cause"),
                "risk": outcome.get("risk"),
                "false_positive_suspicion": outcome.get("false_positive_suspicion"),
            }
        )
    return summaries


async def _calls_of(
    connection: AsyncConnection,
    tms_id: str,
    mission_id: str,
) -> list[dict[str, Any]]:
    """Appels passés **et refusés**.

    Un refus compte autant : il dit au modèle qu'un quota est consommé, et
    qu'il n'obtiendra pas d'appel même en le demandant.
    """
    result = await connection.execute(
        select(
            driver_calls.c.status,
            driver_calls.c.blocked_reason,
            driver_calls.c.requested_by,
            driver_calls.c.reason,
            driver_calls.c.requested_at,
        )
        .where(and_(driver_calls.c.tms_id == tms_id, driver_calls.c.mission_id == mission_id))
        .order_by(driver_calls.c.requested_at.desc())
        .limit(MAX_CALLS)
    )
    return [dict(row) for row in result.mappings()]


async def _decisions_of(
    connection: AsyncConnection,
    tms_id: str,
    mission_id: str,
) -> list[dict[str, Any]]:
    """Décisions d'opérateur consignées sur cette mission (F26, F27).

    C'est le fait le plus utile de tout l'historique : « détour autorisé par le
    client » explique une dérive mieux qu'aucune lecture du TMS, et évite une
    analyse alarmante sur une situation déjà arbitrée.
    """
    result = await connection.execute(
        select(
            audit_log.c.action,
            audit_log.c.actor,
            audit_log.c.detail,
            audit_log.c.occurred_at,
        )
        .where(
            and_(
                audit_log.c.tms_id == tms_id,
                audit_log.c.mission_id == mission_id,
                audit_log.c.action.in_(("operator_decision", "alert_closed_by_operator")),
            )
        )
        .order_by(audit_log.c.occurred_at.desc())
        .limit(MAX_DECISIONS)
    )
    return [dict(row) for row in result.mappings()]


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key))
        counts[value] = counts.get(value, 0) + 1
    return counts
