"""Appel au chauffeur, sous garde-fous.

**La seule action du système vers le monde extérieur.** Tout le reste lit le
TMS ou écrit dans notre base ; ici, un téléphone sonne chez quelqu'un qui
conduit.

## Pourquoi ce n'est pas un outil MCP

La spécification d'architecture range `call_driver` parmi les outils MCP, et
nous en écartons délibérément. Le serveur MCP est *« l'unique frontière avec le
TMS »* — or un appel téléphonique ne passe pas par le TMS, qui est en lecture
seule. Sa trace appartient au domaine de l'agent (contrainte 4 de la
spécification, §25 de l'architecture), et faire écrire le serveur MCP dans la
base de l'orchestrateur contredirait §24. L'appel est donc un outil **local** du
service agent, exposé au modèle au même titre que `submit_assessment`.

Le numéro, lui, est bien lu dans le TMS par MCP : l'annuaire reste au TMS.

## Les garde-fous

Appeler un chauffeur est intrusif, et un LLM peut se tromper. Quatre barrières,
chacune contre une façon précise de nuire :

  - **idempotence** : rejouer une enquête, ou en reprendre une interrompue, ne
    rappelle pas ;
  - **quota par mission** : une mission ne peut pas déclencher une cascade ;
  - **quota et cooldown par chauffeur** : un chauffeur n'est pas harcelé, même
    si plusieurs de ses missions dérivent ;
  - **motif obligatoire** : un appel sans raison écrite est refusé. C'est ce que
    l'on relira si le chauffeur se plaint.

Un appel refusé est **enregistré avec son motif**. Le taire laisserait croire
qu'aucun appel n'a été tenté.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import driver_calls
from shared.channels import Channel, Message
from shared.mapped_dto import NotificationChannel, Severity

logger = logging.getLogger(__name__)

# Longueur minimale d'un motif. Un modèle sommé de justifier écrit « urgent »
# si on le laisse faire : exiger une phrase rend le refus vérifiable.
MIN_REASON_LENGTH = 20


class CallStatus(StrEnum):
    PLACED = "placed"
    FAILED = "failed"
    BLOCKED = "blocked"


# Statuts qui traduisent une tentative réellement remise au fournisseur. Ils
# définissent à la fois l'unicité de la clé d'idempotence et la lecture qui la
# vérifie : les deux doivent dire la même chose, sous peine d'une insertion
# refusée là où le code attend un simple « déjà appelé ».
ATTEMPTED_STATUSES = ("placed", "failed")
_ATTEMPTED_PREDICATE = "status in ('placed', 'failed')"


class BlockedReason(StrEnum):
    """Pourquoi un appel n'a pas été passé. Enregistré, jamais tu."""

    ALREADY_CALLED = "already_called"
    MISSION_QUOTA = "mission_quota"
    DRIVER_QUOTA = "driver_quota"
    DRIVER_COOLDOWN = "driver_cooldown"
    NO_PHONE = "no_phone"
    REASON_TOO_SHORT = "reason_too_short"
    SEVERITY_TOO_LOW = "severity_too_low"


@dataclass(frozen=True, slots=True)
class CallGuardrails:
    """Limites d'appel. Lues sur l'heure TMS, comme les cooldowns d'alerte."""

    max_per_mission: int = 3
    max_per_driver_per_window: int = 2
    driver_window_minutes: int = 60
    driver_cooldown_minutes: int = 15
    # Un appel dérange : en deçà de ce seuil, une notification in-app suffit.
    min_severity: Severity = Severity.HIGH


@dataclass(frozen=True, slots=True)
class CallRequest:
    """Demande d'appel, telle que l'enquêteur ou l'assistant l'exprime."""

    tms_id: str
    mission_id: str
    driver_id: str
    reason: str
    tms_now: datetime
    severity: Severity
    requested_by: str = "investigator"
    alert_id: uuid.UUID | None = None
    investigation_id: uuid.UUID | None = None
    phone: str | None = None
    vehicle_plate: str | None = None
    # Identité imposée de l'appel, quand l'appelant en détient déjà une. C'est
    # le cas d'une validation humaine : la ligne validée *est* l'appel, et deux
    # exécutions de la même validation ne doivent pas faire sonner deux fois.
    idempotency_override: str | None = None

    @property
    def idempotency_key(self) -> str:
        """Identité logique de l'appel.

        Fondée sur l'alerte quand il y en a une : une alerte ne justifie qu'un
        appel, quel que soit le nombre de fois où son enquête est rejouée. Hors
        alerte, l'identifiant de la demande fait office de clé.
        """
        if self.idempotency_override:
            return self.idempotency_override
        if self.alert_id is not None:
            return f"alert:{self.alert_id}:{self.driver_id}"
        if self.investigation_id is not None:
            return f"investigation:{self.investigation_id}:{self.driver_id}"
        return f"adhoc:{uuid.uuid4()}"


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """Issue d'une demande d'appel, rendue au modèle comme à l'opérateur."""

    status: CallStatus
    call_id: uuid.UUID | None = None
    blocked_reason: BlockedReason | None = None
    detail: str = ""
    provider_reference: str | None = None

    @property
    def placed(self) -> bool:
        return self.status is CallStatus.PLACED

    def as_payload(self) -> dict[str, Any]:
        """Forme rendue au modèle.

        Un refus est une **donnée**, pas une erreur : le modèle doit pouvoir
        lire qu'un quota a mordu et en tenir compte dans sa recommandation,
        plutôt que de retenter en boucle.
        """
        return {
            "ok": self.status is not CallStatus.FAILED,
            "status": self.status.value,
            "placed": self.placed,
            "blocked_reason": self.blocked_reason.value if self.blocked_reason else None,
            "detail": self.detail,
            "call_id": str(self.call_id) if self.call_id else None,
        }


class DriverCallService:
    """Passe un appel au chauffeur, si et seulement si les garde-fous l'autorisent."""

    def __init__(
        self,
        channel: Channel,
        *,
        guardrails: CallGuardrails | None = None,
    ) -> None:
        self._channel = channel
        self._guardrails = guardrails or CallGuardrails()

    async def place(self, connection: AsyncConnection, request: CallRequest) -> CallOutcome:
        """Évalue les garde-fous, passe l'appel, enregistre l'issue.

        L'ordre des contrôles est délibéré : les refus qui ne coûtent rien
        d'abord (motif, gravité, numéro), puis ceux qui demandent une lecture
        en base. Un appel refusé pour motif trop court ne doit pas consommer
        une requête de quota.
        """
        blocked = self._static_checks(request) or await self._quota_checks(connection, request)
        if blocked is not None:
            outcome = CallOutcome(
                status=CallStatus.BLOCKED,
                blocked_reason=blocked,
                detail=_explain(blocked, self._guardrails),
            )
            await self._record(connection, request, outcome)
            logger.info("Appel au chauffeur %s refusé (%s)", request.driver_id, blocked.value)
            return outcome

        outcome = await self._dial(request)
        await self._record(connection, request, outcome)
        return outcome

    # --- Contrôles ------------------------------------------------------------

    def _static_checks(self, request: CallRequest) -> BlockedReason | None:
        """Refus qui ne demandent aucune lecture en base."""
        if len(request.reason.strip()) < MIN_REASON_LENGTH:
            return BlockedReason.REASON_TOO_SHORT
        if request.severity < self._guardrails.min_severity:
            return BlockedReason.SEVERITY_TOO_LOW
        if not request.phone:
            return BlockedReason.NO_PHONE
        return None

    async def _quota_checks(
        self,
        connection: AsyncConnection,
        request: CallRequest,
    ) -> BlockedReason | None:
        """Contrôles demandant une lecture en base.

        Les quotas ne comptent que les appels **réellement passés** : un appel
        refusé n'a dérangé personne et n'a donc pas à peser sur le quota.
        """
        guardrails = self._guardrails

        # Seules les **tentatives réelles** comptent : un appel refusé n'a pas
        # fait sonner le téléphone, et refuser indéfiniment au motif qu'un
        # refus a été tracé rendrait un cooldown définitif.
        existing = await connection.execute(
            select(driver_calls.c.id).where(
                and_(
                    driver_calls.c.idempotency_key == request.idempotency_key,
                    driver_calls.c.status.in_(ATTEMPTED_STATUSES),
                )
            )
        )
        if existing.first() is not None:
            return BlockedReason.ALREADY_CALLED

        mission_calls = await connection.execute(
            select(func.count())
            .select_from(driver_calls)
            .where(
                and_(
                    driver_calls.c.tms_id == request.tms_id,
                    driver_calls.c.mission_id == request.mission_id,
                    driver_calls.c.status == CallStatus.PLACED.value,
                )
            )
        )
        if int(mission_calls.scalar_one()) >= guardrails.max_per_mission:
            return BlockedReason.MISSION_QUOTA

        window_start = request.tms_now - timedelta(minutes=guardrails.driver_window_minutes)
        recent = await connection.execute(
            select(driver_calls.c.requested_at)
            .where(
                and_(
                    driver_calls.c.tms_id == request.tms_id,
                    driver_calls.c.driver_id == request.driver_id,
                    driver_calls.c.status == CallStatus.PLACED.value,
                    driver_calls.c.requested_at >= window_start,
                )
            )
            .order_by(driver_calls.c.requested_at.desc())
        )
        moments = [row.requested_at for row in recent]

        if len(moments) >= guardrails.max_per_driver_per_window:
            return BlockedReason.DRIVER_QUOTA
        if moments and moments[0] > request.tms_now - timedelta(minutes=guardrails.driver_cooldown_minutes):
            return BlockedReason.DRIVER_COOLDOWN
        return None

    # --- Acheminement ---------------------------------------------------------

    async def _dial(self, request: CallRequest) -> CallOutcome:
        assert request.phone is not None  # garanti par les contrôles statiques
        message = Message(
            channel=NotificationChannel.CALL,
            address=request.phone,
            title="Appel du suivi d'exploitation",
            body=request.reason,
            severity=request.severity,
            alert_id=str(request.alert_id) if request.alert_id else "",
            mission_id=request.mission_id,
            recipient_id=request.driver_id,
            metadata={"tms_id": request.tms_id, "requested_by": request.requested_by},
        )
        try:
            result = await self._channel.send(message, sent_at=datetime.now(UTC))
        except Exception as exception:
            logger.exception("Fournisseur de téléphonie en erreur")
            return CallOutcome(status=CallStatus.FAILED, detail=f"erreur du fournisseur : {exception}")

        if not result.delivered:
            return CallOutcome(
                status=CallStatus.FAILED,
                detail=result.failure_reason or "appel non abouti",
            )
        return CallOutcome(
            status=CallStatus.PLACED,
            call_id=uuid.uuid4(),
            detail="appel passé",
            provider_reference=result.reference,
        )

    # --- Trace ----------------------------------------------------------------

    async def _record(
        self,
        connection: AsyncConnection,
        request: CallRequest,
        outcome: CallOutcome,
    ) -> None:
        """Enregistre l'appel, y compris refusé.

        `ON CONFLICT DO NOTHING` sur la clé d'idempotence : deux demandes
        concurrentes pour la même alerte ne produisent qu'une ligne, et la
        seconde lira `already_called` au contrôle suivant.
        """
        call_id = outcome.call_id or uuid.uuid4()
        statement = (
            pg_insert(driver_calls)
            .values(
                id=call_id,
                idempotency_key=request.idempotency_key,
                tms_id=request.tms_id,
                mission_id=request.mission_id,
                driver_id=request.driver_id,
                alert_id=request.alert_id,
                investigation_id=request.investigation_id,
                requested_by=request.requested_by,
                reason=request.reason,
                status=outcome.status.value,
                blocked_reason=(outcome.blocked_reason.value if outcome.blocked_reason else None),
                phone=request.phone,
                provider=getattr(self._channel, "name", None),
                provider_reference=outcome.provider_reference,
                failure_reason=outcome.detail if outcome.status is CallStatus.FAILED else None,
                requested_at=request.tms_now,
                placed_at=request.tms_now if outcome.placed else None,
            )
            # Prédicat **littéral** et non paramétré : au-delà de cinq
            # exécutions, PostgreSQL bascule sur un plan générique et un
            # paramètre lié cesse de correspondre à l'index partiel, qui n'est
            # alors plus reconnu — l'insertion échouerait sur l'unicité au lieu
            # de ne rien faire.
            .on_conflict_do_nothing(
                index_elements=[driver_calls.c.idempotency_key],
                index_where=text(_ATTEMPTED_PREDICATE),
            )
        )
        await connection.execute(statement)


def _explain(reason: BlockedReason, guardrails: CallGuardrails) -> str:
    """Message destiné au modèle : dire pourquoi, pour qu'il n'insiste pas."""
    return {
        BlockedReason.ALREADY_CALLED: "Ce chauffeur a déjà été appelé pour cette alerte.",
        BlockedReason.MISSION_QUOTA: (
            f"Quota de {guardrails.max_per_mission} appels atteint pour cette mission."
        ),
        BlockedReason.DRIVER_QUOTA: (
            f"Quota de {guardrails.max_per_driver_per_window} appels atteint pour ce "
            f"chauffeur sur {guardrails.driver_window_minutes} minutes."
        ),
        BlockedReason.DRIVER_COOLDOWN: (
            f"Ce chauffeur a été appelé il y a moins de {guardrails.driver_cooldown_minutes} minutes."
        ),
        BlockedReason.NO_PHONE: "Aucun numéro renseigné pour ce chauffeur dans le TMS.",
        BlockedReason.REASON_TOO_SHORT: (
            f"Le motif doit faire au moins {MIN_REASON_LENGTH} caractères et expliquer "
            "ce qui est demandé au chauffeur."
        ),
        BlockedReason.SEVERITY_TOO_LOW: (
            f"Un appel n'est autorisé qu'à partir de la gravité "
            f"{guardrails.min_severity.value}. Recommander une notification in-app."
        ),
    }[reason]
