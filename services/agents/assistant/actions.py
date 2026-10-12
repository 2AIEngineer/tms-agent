"""Les trois actions de l'assistant, et rien de plus (F26).

Clore une alerte, appeler un chauffeur, consigner une décision. Trois gestes
qu'un opérateur accomplirait lui-même, et une liste **fermée** : ni notifier,
ni alerter, ni écrire dans le TMS. Alerter est le travail des règles — un LLM
qui pourrait émettre une alerte rendrait la détection non déterministe, et
c'est la propriété que tout le système protège.

Trois garde-fous encadrent ces outils :

  - **ils ne sont présentés au modèle que si l'opérateur a autorisé d'agir.**
    Pas de refus à l'exécution : un outil absent ne peut pas être appelé par
    zèle, et n'occupe pas la fenêtre de contexte le reste du temps ;
  - **les gestes irréversibles sont *préparés*, puis validés** (voir
    `pending`). Le modèle ne les exécute pas : il les propose, l'opérateur
    tranche, et ce sont les arguments figés qui s'exécutent ;
  - **chaque action est journalisée avec son demandeur** (F27). « L'assistant a
    clos l'alerte » ne suffirait pas : il faut savoir qui le lui a demandé.

Un échec est **rendu** et non levé : un quota d'appel atteint ou une alerte
déjà close sont des réponses, que l'opérateur doit lire.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import alerts
from services.monitoring.alerts.repository import AlertRepository
from shared.mapped_dto import (
    ActionKind,
    AlertStatus,
    AssistantRequest,
    ExecutedAction,
    ResolutionReason,
    Severity,
)
from shared.utils.db import Database

from ..integrations.mcp_client import AgentMcpClient
from ..operations.driver_call import CallRequest, DriverCallService
from .tools import ACTION_TOOL_DEFINITIONS

logger = logging.getLogger(__name__)


def _reason_too_short() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "message": (
                f"Un motif d'au moins {MIN_REASON_LENGTH} caractères est requis : "
                "il est conservé pour l'audit et montré à l'opérateur."
            )
        },
    }


# Motif minimal d'une action. Même exigence que pour l'appel au chauffeur : une
# clôture sans justification écrite est inexploitable en audit.
MIN_REASON_LENGTH = 10


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Ce dont une action a besoin en plus de ses arguments.

    Un contexte explicite et non la requête complète : à la validation, la
    requête d'origine n'existe plus — seule la ligne validée subsiste, et
    l'exécution ne doit dépendre de rien d'autre.
    """

    operator_id: str
    mission_id: str | None = None
    alert_id: uuid.UUID | None = None
    # Identité imposée de l'appel au chauffeur, quand une validation humaine la
    # porte déjà : deux exécutions de la même validation ne sonnent qu'une fois.
    idempotency_override: str | None = None


@dataclass(frozen=True, slots=True)
class Proposal:
    """Un geste prêt à être soumis à la validation de l'opérateur.

    `summary` est construit **par le code**, à partir des arguments et de la
    base. La prose du modèle est utile mais ne fait pas foi : l'opérateur doit
    lire ce qui va réellement s'exécuter.
    """

    kind: ActionKind
    arguments: dict[str, Any]
    summary: str
    reason: str
    tms_id: str | None = None
    mission_id: str | None = None
    alert_id: uuid.UUID | None = None


class AssistantActions:
    """Prépare, exécute et journalise les actions demandées par l'opérateur."""

    def __init__(
        self,
        database: Database,
        mcp: AgentMcpClient,
        *,
        driver_calls: DriverCallService | None = None,
    ) -> None:
        self._database = database
        self._mcp = mcp
        self._driver_calls = driver_calls

    def definitions(self) -> list[dict[str, Any]]:
        """Outils d'action, moins ceux qui n'ont pas de service derrière eux.

        L'appel au chauffeur disparaît si la téléphonie est coupée
        (`DRIVER_CALL_ENABLED=false`) : mieux vaut un outil absent qu'un outil
        qui refuse.
        """
        return [
            definition
            for name, definition in ACTION_TOOL_DEFINITIONS.items()
            if name != ActionKind.CALL_DRIVER.value or self._driver_calls is not None
        ]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(definition["function"]["name"] for definition in self.definitions())

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        request: AssistantRequest,
    ) -> tuple[dict[str, Any], ExecutedAction | None]:
        """Exécute une action **sans validation** et rend (charge utile, trace).

        Réservé aux gestes qui ne sortent pas du système — voir
        `REQUIRES_CONFIRMATION`. Les autres passent par `prepare` puis
        `execute`.
        """
        return await self.execute(
            name,
            arguments,
            ActionContext(
                operator_id=request.operator_id,
                mission_id=request.mission_id,
                alert_id=request.alert_id,
            ),
        )

    async def prepare(
        self,
        name: str,
        arguments: dict[str, Any],
        request: AssistantRequest,
    ) -> tuple[dict[str, Any] | None, Proposal | None]:
        """Valide une demande d'action et en fait une proposition lisible.

        Les contrôles qui peuvent se faire **avant** de solliciter l'opérateur
        le sont ici : un motif trop court, une alerte inconnue ou déjà close,
        une mission absente. Lui demander de valider un geste qui échouera de
        toute façon serait une perte de temps, et une perte de confiance.

        Rend soit une charge d'erreur à remettre au modèle, soit la
        proposition.
        """
        reason = str(arguments.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            return (_reason_too_short(), None)

        if name == ActionKind.CLOSE_ALERT.value:
            return await self._prepare_close_alert(arguments, reason)
        if name == ActionKind.CALL_DRIVER.value:
            return await self._prepare_call_driver(arguments, reason)
        return ({"ok": False, "error": {"message": f"Action inconnue : {name}"}}, None)

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ActionContext,
    ) -> tuple[dict[str, Any], ExecutedAction | None]:
        """Exécute une action, à partir de ses seuls arguments et du contexte."""
        reason = str(arguments.get("reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            return (_reason_too_short(), None)

        try:
            if name == ActionKind.CLOSE_ALERT.value:
                return await self._close_alert(arguments, reason, context)
            if name == ActionKind.CALL_DRIVER.value:
                return await self._call_driver(arguments, reason, context)
            if name == ActionKind.LOG_DECISION.value:
                return await self._log_decision(arguments, reason, context)
        except Exception as exception:
            logger.exception("Action %s en échec", name)
            return ({"ok": False, "error": {"message": f"Action en échec : {exception}"}}, None)

        return ({"ok": False, "error": {"message": f"Action inconnue : {name}"}}, None)

    # --- Préparation des propositions ----------------------------------------

    async def _prepare_close_alert(
        self,
        arguments: dict[str, Any],
        reason: str,
    ) -> tuple[dict[str, Any] | None, Proposal | None]:
        """Vérifie que l'alerte existe et est ouverte, puis décrit la clôture."""
        raw_id = str(arguments.get("alert_id") or "").strip()
        try:
            alert_id = uuid.UUID(raw_id)
        except ValueError:
            return ({"ok": False, "error": {"message": "`alert_id` n'est pas un identifiant valide."}}, None)

        async with self._database.connection() as connection:
            alert = await self._load_alert(connection, alert_id, with_title=True)
        if alert is None:
            return ({"ok": False, "error": {"message": "Alerte introuvable."}}, None)
        if alert["status"] != AlertStatus.OPEN.value:
            # Inutile de déranger l'opérateur pour un geste déjà accompli.
            return (
                {
                    "ok": True,
                    "closed": False,
                    "detail": f"Cette alerte est déjà close ({alert['status']}).",
                },
                None,
            )

        return (
            None,
            Proposal(
                kind=ActionKind.CLOSE_ALERT,
                arguments={"alert_id": str(alert_id), "reason": reason},
                summary=(
                    f"Clore l'alerte « {alert['title']} » "
                    f"(mission {alert['mission_id']}, TMS {alert['tms_id']}, "
                    f"règle {alert['rule_id']})"
                ),
                reason=reason,
                tms_id=alert["tms_id"],
                mission_id=alert["mission_id"],
                alert_id=alert_id,
            ),
        )

    async def _prepare_call_driver(
        self,
        arguments: dict[str, Any],
        reason: str,
    ) -> tuple[dict[str, Any] | None, Proposal | None]:
        """Identifie le chauffeur avant de proposer de l'appeler.

        Nommer la personne dans la proposition n'est pas cosmétique : un
        opérateur qui valide un appel doit savoir **qui** va être dérangé.
        """
        mission_id = str(arguments.get("mission_id") or "").strip()
        tms_id = str(arguments.get("tms_id") or "").strip()
        if not mission_id or not tms_id:
            return ({"ok": False, "error": {"message": "`tms_id` et `mission_id` sont requis."}}, None)
        if self._driver_calls is None:
            return (
                {"ok": False, "error": {"message": "La téléphonie n'est pas raccordée."}},
                None,
            )

        snapshot = await self._snapshot(tms_id, mission_id)
        driver = snapshot.get("driver") if snapshot else None
        if not isinstance(driver, dict):
            return (
                {
                    "ok": True,
                    "placed": False,
                    "detail": "Chauffeur non identifiable sur cette mission.",
                },
                None,
            )
        if not driver.get("phone"):
            return (
                {
                    "ok": True,
                    "placed": False,
                    "detail": "Aucun numéro renseigné pour ce chauffeur dans le TMS.",
                },
                None,
            )

        reference = str(snapshot.get("reference") or mission_id) if snapshot else mission_id
        return (
            None,
            Proposal(
                kind=ActionKind.CALL_DRIVER,
                arguments={"tms_id": tms_id, "mission_id": mission_id, "reason": reason},
                summary=(
                    f"Appeler {driver.get('name') or 'le chauffeur'} "
                    f"({driver['phone']}) — mission {reference}"
                ),
                reason=reason,
                tms_id=tms_id,
                mission_id=mission_id,
            ),
        )

    # --- Clôture d'alerte -----------------------------------------------------

    async def _close_alert(
        self,
        arguments: dict[str, Any],
        reason: str,
        context: ActionContext,
    ) -> tuple[dict[str, Any], ExecutedAction | None]:
        """Clôt une alerte à la demande de l'opérateur.

        `ResolutionReason.OPERATOR` et non `condition_cleared` : la condition
        peut très bien être encore vraie. Confondre les deux ferait croire, en
        relisant, que le système avait constaté un retour à la normale.
        """
        raw_id = str(arguments.get("alert_id") or "").strip()
        try:
            alert_id = uuid.UUID(raw_id)
        except ValueError:
            return ({"ok": False, "error": {"message": "`alert_id` n'est pas un identifiant valide."}}, None)

        async with self._database.transaction() as connection:
            alert = await self._load_alert(connection, alert_id)
            if alert is None:
                return ({"ok": False, "error": {"message": "Alerte introuvable."}}, None)
            if alert["status"] != AlertStatus.OPEN.value:
                # Pas une erreur : l'opérateur doit savoir qu'elle l'était déjà.
                action = ExecutedAction(
                    kind=ActionKind.CLOSE_ALERT,
                    succeeded=False,
                    detail=f"déjà close ({alert['status']})",
                    alert_id=alert_id,
                    mission_id=alert["mission_id"],
                )
                return ({"ok": True, "closed": False, "detail": action.detail}, action)

            # Heure TMS : une clôture se date sur l'horloge du TMS, comme la
            # détection qu'elle solde (F3).
            closed_at = await self._tms_time(alert["tms_id"])
            repository = AlertRepository()
            closed = await repository.resolve(
                connection,
                [alert_id],
                resolved_at=closed_at,
                reason=ResolutionReason.OPERATOR,
            )
            await repository.append_audit(
                connection,
                [
                    {
                        "action": "alert_closed_by_operator",
                        "tms_id": alert["tms_id"],
                        "mission_id": alert["mission_id"],
                        "alert_id": alert_id,
                        "rule_id": alert["rule_id"],
                        "actor": context.operator_id,
                        "detail": {"reason": reason, "via": "assistant"},
                        "occurred_at": closed_at,
                    }
                ],
            )

        action = ExecutedAction(
            kind=ActionKind.CLOSE_ALERT,
            succeeded=bool(closed),
            detail="alerte close à la demande de l'opérateur",
            alert_id=alert_id,
            mission_id=alert["mission_id"],
        )
        return ({"ok": True, "closed": bool(closed), "closed_at": closed_at.isoformat()}, action)

    # --- Appel au chauffeur ---------------------------------------------------

    async def _call_driver(
        self,
        arguments: dict[str, Any],
        reason: str,
        context: ActionContext,
    ) -> tuple[dict[str, Any], ExecutedAction | None]:
        """Appelle le chauffeur d'une mission, sous les mêmes garde-fous.

        Les quotas sont partagés avec l'enquêteur, et c'est voulu : un chauffeur
        déjà appelé par une enquête ne doit pas l'être une seconde fois parce
        que la demande vient cette fois d'un opérateur.
        """
        assert self._driver_calls is not None  # garanti par `definitions()`
        mission_id = str(arguments.get("mission_id") or "").strip()
        tms_id = str(arguments.get("tms_id") or "").strip()
        if not mission_id or not tms_id:
            return ({"ok": False, "error": {"message": "`tms_id` et `mission_id` sont requis."}}, None)

        snapshot = await self._snapshot(tms_id, mission_id)
        if snapshot is None or snapshot.get("driver") is None:
            return (
                {
                    "ok": True,
                    "placed": False,
                    "detail": "Chauffeur non identifiable sur cette mission.",
                },
                ExecutedAction(
                    kind=ActionKind.CALL_DRIVER,
                    succeeded=False,
                    detail="chauffeur non identifiable",
                    mission_id=mission_id,
                ),
            )

        driver = snapshot["driver"]
        call = CallRequest(
            tms_id=tms_id,
            mission_id=mission_id,
            driver_id=str(driver["id"]),
            reason=reason,
            tms_now=datetime.fromisoformat(str(snapshot["observed_at"])),
            # Un opérateur qui demande un appel a décidé qu'il était justifié :
            # le seuil de gravité, qui protège le chauffeur d'un excès de zèle
            # du modèle, n'a pas à s'appliquer à une décision humaine. Les
            # quotas, eux, continuent de s'appliquer.
            severity=Severity.CRITICAL,
            requested_by="assistant",
            phone=driver.get("phone"),
            idempotency_override=context.idempotency_override,
        )
        async with self._database.transaction() as connection:
            outcome = await self._driver_calls.place(connection, call)

        return (
            outcome.as_payload(),
            ExecutedAction(
                kind=ActionKind.CALL_DRIVER,
                succeeded=outcome.placed,
                detail=outcome.detail,
                mission_id=mission_id,
            ),
        )

    # --- Décision consignée ---------------------------------------------------

    async def _log_decision(
        self,
        arguments: dict[str, Any],
        reason: str,
        context: ActionContext,
    ) -> tuple[dict[str, Any], ExecutedAction | None]:
        """Consigne une décision d'exploitation dans le journal (F26, F27).

        Utile précisément quand rien d'autre n'est fait : « détour autorisé,
        aucune action » est une information, et sans trace le cycle suivant
        rouvrirait la discussion.
        """
        mission_id = str(arguments.get("mission_id") or "").strip() or context.mission_id
        tms_id = str(arguments.get("tms_id") or "").strip() or None
        occurred_at = await self._tms_time(tms_id) if tms_id else datetime.now(tz=UTC)
        async with self._database.transaction() as connection:
            await AlertRepository().append_audit(
                connection,
                [
                    {
                        "action": "operator_decision",
                        "tms_id": tms_id,
                        "mission_id": mission_id,
                        "alert_id": context.alert_id,
                        "actor": context.operator_id,
                        "detail": {"decision": reason, "via": "assistant"},
                        "occurred_at": occurred_at,
                    }
                ],
            )
        return (
            {"ok": True, "logged": True},
            ExecutedAction(
                kind=ActionKind.LOG_DECISION,
                succeeded=True,
                detail=reason,
                mission_id=mission_id,
                alert_id=context.alert_id,
            ),
        )

    # --- Lectures d'appui -----------------------------------------------------

    @staticmethod
    async def _load_alert(
        connection: AsyncConnection,
        alert_id: uuid.UUID,
        *,
        with_title: bool = False,
    ) -> dict[str, Any] | None:
        columns = [
            alerts.c.id,
            alerts.c.tms_id,
            alerts.c.mission_id,
            alerts.c.rule_id,
            alerts.c.status,
        ]
        if with_title:
            # Lu pour la **description** soumise à l'opérateur : « clore
            # l'alerte 7f2a… » ne lui dit rien, « clore l'alerte Dérive
            # d'itinéraire, mission msn_1 » lui dit tout.
            columns.append(alerts.c.title)
        result = await connection.execute(select(*columns).where(alerts.c.id == alert_id))
        row = result.mappings().first()
        return dict(row) if row else None

    async def _tms_time(self, tms_id: str) -> datetime:
        """Heure du TMS, lue par MCP.

        En cas d'échec, l'horloge du serveur sert de repli, et c'est un pis-aller
        assumé : une clôture doit aboutir même si le TMS répond mal, et un
        horodatage approché vaut mieux qu'une alerte laissée ouverte contre la
        volonté de l'opérateur. Le cas est journalisé.
        """
        result = await self._mcp.call("get_tms_time", {"tms_id": tms_id})
        raw = result.payload.get("tms_time") if result.ok else None
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        logger.warning("Heure TMS de %s illisible : horodatage sur l'horloge du serveur", tms_id)
        return datetime.now(tz=UTC)

    async def _snapshot(self, tms_id: str, mission_id: str) -> dict[str, Any] | None:
        result = await self._mcp.call("get_mission_snapshot", {"tms_id": tms_id, "mission_id": mission_id})
        raw = result.payload.get("snapshot") if result.ok else None
        return raw if isinstance(raw, dict) else None
