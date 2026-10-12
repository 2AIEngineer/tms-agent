"""Validation humaine des actions de l'assistant.

C'est la pièce qui rend l'assistant utilisable sur des gestes irréversibles.
Le modèle **prépare** une action ; l'opérateur la **valide** ; le système
l'exécute **telle qu'elle a été montrée**.

## Pourquoi la validation porte sur une ligne, et non sur un tour

Autoriser « agir » pour un tour de conversation laissait le modèle replanifier
librement : l'opérateur confirmait « clore l'alerte A » et rien, dans la
structure, ne garantissait que ce serait A. Ici, la confirmation désigne un
identifiant, et l'exécution relit les arguments figés en base. Le modèle ne
repasse pas.

C'est le contrat `interrupt`/`resume` d'un graphe d'agents, mais porté par des
lignes que l'interface peut **interroger** : « qu'attend-on de moi ? » est une
question qu'un opérateur pose en ouvrant son écran, pas seulement dans la
continuité d'une requête HTTP.

## Les cinq propriétés que ce module garantit

  - **usage unique.** La transition `proposed -> executing` est un `UPDATE …
    WHERE status = 'proposed' RETURNING` : deux onglets qui valident en même
    temps n'exécutent qu'une fois ;
  - **péremption.** Un accord donné il y a deux heures ne dit rien de la
    situation actuelle d'une mission ; passé son délai, l'action est inerte ;
  - **propriété.** Seul l'opérateur à qui l'action a été proposée peut la
    trancher. Un collègue, même mieux habilité, n'a pas le mandat ;
  - **survie à une panne.** L'état est en base. Un processus tué entre la
    validation et l'exécution laisse une ligne `executing` que le balayage
    solde en `failed`, avec son motif — jamais un silence ;
  - **traçabilité.** Proposition, décision et exécution sont datées et
    attribuées (F27).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import assistant_pending_actions as table
from shared.mapped_dto import ActionKind, PendingAction, PendingActionStatus
from shared.utils.serialization import to_jsonable

logger = logging.getLogger(__name__)

# Durée de vie d'une proposition. Assez longue pour qu'un opérateur interrompu
# par un appel téléphonique revienne la valider, assez courte pour qu'un
# accord ne survive pas à la situation qui le justifiait.
DEFAULT_TTL = timedelta(minutes=15)

# Au-delà, une action restée `executing` est considérée abandonnée par le
# processus qui l'avait prise. Généreux : une exécution lente — un appel
# téléphonique passe par un fournisseur — est normale.
EXECUTION_LEASE = timedelta(minutes=5)

# Actions qui exigent une validation explicite avant exécution.
#
# Le partage n'est pas une commodité : il sépare ce qui **sort du système** de
# ce qui reste dedans. Clore une alerte retire une anomalie de l'écran des
# collègues ; appeler fait sonner le téléphone de quelqu'un qui conduit. Les
# deux se valident. Consigner une décision n'écrit qu'une ligne de journal que
# l'opérateur vient de dicter : exiger un second clic serait une friction sans
# contrepartie, et la friction inutile finit par être cliquée sans lire.
REQUIRES_CONFIRMATION: frozenset[ActionKind] = frozenset({ActionKind.CLOSE_ALERT, ActionKind.CALL_DRIVER})


@dataclass(frozen=True, slots=True)
class ClaimedAction:
    """Action validée et prise, prête à être exécutée.

    Porte les arguments **figés** : c'est ce que l'exécuteur reçoit, et non ce
    que le modèle aurait redit.
    """

    id: uuid.UUID
    kind: ActionKind
    arguments: dict[str, Any]
    reason: str
    summary: str
    conversation_id: uuid.UUID
    operator_id: str


class PendingActionRepository:
    """Lecture et transitions des actions en attente de validation."""

    async def propose(
        self,
        connection: AsyncConnection,
        *,
        conversation_id: uuid.UUID,
        operator_id: str,
        kind: ActionKind,
        arguments: dict[str, Any],
        summary: str,
        reason: str,
        tms_id: str | None = None,
        mission_id: str | None = None,
        alert_id: uuid.UUID | None = None,
        ttl: timedelta = DEFAULT_TTL,
        now: datetime | None = None,
    ) -> PendingAction:
        """Enregistre un geste préparé et le rend à l'interface."""
        moment = now or datetime.now(UTC)
        action_id = uuid.uuid4()
        expires_at = moment + ttl
        await connection.execute(
            insert(table).values(
                id=action_id,
                conversation_id=conversation_id,
                operator_id=operator_id,
                kind=kind.value,
                arguments=to_jsonable(arguments),
                summary=summary,
                reason=reason,
                tms_id=tms_id,
                mission_id=mission_id,
                alert_id=alert_id,
                status=PendingActionStatus.PROPOSED.value,
                expires_at=expires_at,
            )
        )
        return PendingAction(
            id=action_id,
            kind=kind,
            status=PendingActionStatus.PROPOSED,
            summary=summary,
            reason=reason,
            conversation_id=conversation_id,
            arguments={key: str(value) for key, value in arguments.items()},
            alert_id=alert_id,
            mission_id=mission_id,
            tms_id=tms_id,
            expires_at=expires_at,
            created_at=moment,
        )

    async def waiting(
        self,
        connection: AsyncConnection,
        operator_id: str,
        *,
        limit: int = 20,
        now: datetime | None = None,
    ) -> list[PendingAction]:
        """Ce qui attend cet opérateur, le plus ancien d'abord.

        Les propositions périmées sont écartées à la lecture **en plus** d'être
        balayées : entre deux balayages, l'interface ne doit pas proposer de
        valider une action qui ne s'exécutera pas.
        """
        moment = now or datetime.now(UTC)
        result = await connection.execute(
            select(table)
            .where(
                and_(
                    table.c.operator_id == operator_id,
                    table.c.status == PendingActionStatus.PROPOSED.value,
                    table.c.expires_at > moment,
                )
            )
            .order_by(table.c.created_at)
            .limit(limit)
        )
        return [_to_contract(row) for row in result.mappings()]

    async def get(
        self,
        connection: AsyncConnection,
        action_id: uuid.UUID,
        *,
        operator_id: str | None = None,
    ) -> PendingAction | None:
        """Lit une action, éventuellement restreinte à son propriétaire."""
        query = select(table).where(table.c.id == action_id)
        if operator_id is not None:
            query = query.where(table.c.operator_id == operator_id)
        row = (await connection.execute(query)).mappings().first()
        return _to_contract(row) if row else None

    async def claim(
        self,
        connection: AsyncConnection,
        action_id: uuid.UUID,
        *,
        operator_id: str,
        now: datetime | None = None,
    ) -> ClaimedAction | None:
        """Valide et prend l'action, en une seule instruction.

        `UPDATE … WHERE status = 'proposed' … RETURNING` : c'est l'atomicité de
        cette transition qui garantit l'usage unique. Deux onglets, deux
        requêtes concurrentes, un seul gagnant — et le second lira que l'action
        n'est plus en attente, ce qui est exact.

        Rend `None` si l'action n'existe pas, appartient à quelqu'un d'autre,
        est périmée, ou a déjà été tranchée. Les quatre cas se distinguent par
        une lecture (`get`), que l'appelant fait pour expliquer le refus.
        """
        moment = now or datetime.now(UTC)
        statement = (
            update(table)
            .where(
                and_(
                    table.c.id == action_id,
                    table.c.operator_id == operator_id,
                    table.c.status == PendingActionStatus.PROPOSED.value,
                    table.c.expires_at > moment,
                )
            )
            .values(
                status=PendingActionStatus.EXECUTING.value,
                decided_at=moment,
                decided_by=operator_id,
                claimed_at=moment,
            )
            .returning(
                table.c.id,
                table.c.kind,
                table.c.arguments,
                table.c.reason,
                table.c.summary,
                table.c.conversation_id,
                table.c.operator_id,
            )
        )
        row = (await connection.execute(statement)).mappings().first()
        if row is None:
            return None
        return ClaimedAction(
            id=row["id"],
            kind=ActionKind(row["kind"]),
            arguments=dict(row["arguments"] or {}),
            reason=row["reason"],
            summary=row["summary"],
            conversation_id=row["conversation_id"],
            operator_id=row["operator_id"],
        )

    async def reject(
        self,
        connection: AsyncConnection,
        action_id: uuid.UUID,
        *,
        operator_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Refuse une action. Même atomicité que la validation."""
        moment = now or datetime.now(UTC)
        result = await connection.execute(
            update(table)
            .where(
                and_(
                    table.c.id == action_id,
                    table.c.operator_id == operator_id,
                    table.c.status == PendingActionStatus.PROPOSED.value,
                )
            )
            .values(
                status=PendingActionStatus.REJECTED.value,
                decided_at=moment,
                decided_by=operator_id,
            )
            .returning(table.c.id)
        )
        return result.first() is not None

    async def record_result(
        self,
        connection: AsyncConnection,
        action_id: uuid.UUID,
        *,
        succeeded: bool,
        payload: dict[str, Any],
        failure_reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Solde l'exécution, réussie ou non."""
        await connection.execute(
            update(table)
            .where(table.c.id == action_id)
            .values(
                status=(
                    PendingActionStatus.EXECUTED.value if succeeded else PendingActionStatus.FAILED.value
                ),
                result=to_jsonable(payload),
                failure_reason=failure_reason,
                executed_at=now or datetime.now(UTC),
            )
        )

    async def sweep(
        self,
        connection: AsyncConnection,
        *,
        now: datetime | None = None,
        lease: timedelta = EXECUTION_LEASE,
    ) -> dict[str, int]:
        """Solde ce que le temps a tranché : propositions périmées, exécutions perdues.

        Deux soldes, et le second compte autant que le premier : une action
        prise par un processus qui est mort resterait `executing` pour
        toujours. L'opérateur lirait « en cours » sur un geste que plus
        personne ne tient.
        """
        moment = now or datetime.now(UTC)
        expired = await connection.execute(
            update(table)
            .where(
                and_(
                    table.c.status == PendingActionStatus.PROPOSED.value,
                    table.c.expires_at <= moment,
                )
            )
            .values(status=PendingActionStatus.EXPIRED.value)
            .returning(table.c.id)
        )
        abandoned = await connection.execute(
            update(table)
            .where(
                and_(
                    table.c.status == PendingActionStatus.EXECUTING.value,
                    table.c.claimed_at < moment - lease,
                )
            )
            .values(
                status=PendingActionStatus.FAILED.value,
                failure_reason=(
                    "Exécution interrompue : le service a été arrêté entre la "
                    "validation et l'exécution. L'action n'a pas été effectuée."
                ),
                executed_at=moment,
            )
            .returning(table.c.id)
        )
        counts = {
            "expired": len(list(expired)),
            "abandoned": len(list(abandoned)),
        }
        if any(counts.values()):
            logger.info(
                "Actions soldées — %d périmée(s), %d exécution(s) perdue(s)",
                counts["expired"],
                counts["abandoned"],
            )
        return counts


def _to_contract(row: Any) -> PendingAction:
    return PendingAction(
        id=row["id"],
        kind=ActionKind(row["kind"]),
        status=PendingActionStatus(row["status"]),
        summary=row["summary"],
        reason=row["reason"],
        conversation_id=row["conversation_id"],
        arguments={key: str(value) for key, value in (row["arguments"] or {}).items()},
        alert_id=row["alert_id"],
        mission_id=row["mission_id"],
        tms_id=row["tms_id"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )
