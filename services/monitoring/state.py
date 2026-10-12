"""Battement de cœur de la boucle de surveillance.

La panne la plus grave de ce système est **silencieuse** : un worker arrêté ne
produit aucune alerte, ce qui est indiscernable d'une flotte sans anomalie.
Personne ne s'en aperçoit avant qu'un retard ne soit signalé par un client.

Ce module enregistre donc le dernier cycle réussi, et permet de répondre à la
seule question qui compte pour l'exploitation : « la surveillance tourne-t-elle
encore ? »
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import monitoring_state
from shared.utils.serialization import to_jsonable

LAST_CYCLE_KEY = "last_cycle"


def cycle_key(tms_id: str) -> str:
    """Clé du battement de cœur d'un TMS.

    Un battement global masquerait un client silencieux dès lors qu'un autre
    continue de tourner — exactement la panne que ce mécanisme doit révéler.
    """
    return f"{LAST_CYCLE_KEY}:{tms_id}"


# Une alarme d'exploitation doit se déclencher si aucun cycle n'a réussi depuis
# ce nombre d'intervalles. Trois, et non un : un cycle isolé peut échouer sur
# une coupure réseau sans que la surveillance soit en panne.
STALE_CYCLE_INTERVALS = 3


@dataclass(frozen=True, slots=True)
class Heartbeat:
    updated_at: datetime
    detail: dict[str, Any]

    def is_stale(self, *, poll_interval_s: int, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        limit = timedelta(seconds=poll_interval_s * STALE_CYCLE_INTERVALS)
        return reference - self.updated_at > limit


class MonitoringStateRepository:
    async def record_cycle(
        self,
        connection: AsyncConnection,
        detail: dict[str, Any],
        *,
        key: str = LAST_CYCLE_KEY,
    ) -> None:
        """Enregistre le passage d'un cycle, en écrasant le précédent.

        Une seule ligne par clé : l'historique détaillé appartient au journal
        d'audit, cette table ne répond qu'à « quand pour la dernière fois ? ».
        """
        statement = (
            pg_insert(monitoring_state)
            .values(key=key, value=to_jsonable(detail), updated_at=func.now())
            .on_conflict_do_update(
                index_elements=[monitoring_state.c.key],
                set_={"value": to_jsonable(detail), "updated_at": func.now()},
            )
        )
        await connection.execute(statement)

    async def last_cycle(
        self,
        connection: AsyncConnection,
        *,
        key: str = LAST_CYCLE_KEY,
    ) -> Heartbeat | None:
        result = await connection.execute(
            select(monitoring_state.c.updated_at, monitoring_state.c.value).where(
                monitoring_state.c.key == key
            )
        )
        row = result.mappings().first()
        if row is None:
            return None
        return Heartbeat(updated_at=row["updated_at"], detail=dict(row["value"] or {}))

    async def all_cycles(self, connection: AsyncConnection) -> list[dict[str, Any]]:
        """Dernier battement de **chaque** TMS surveillé.

        Lu par l'interface d'exploitation : un TMS silencieux doit se voir à
        côté de ceux qui tournent, et un battement global le masquerait.
        """
        result = await connection.execute(
            select(
                monitoring_state.c.key,
                monitoring_state.c.updated_at,
                monitoring_state.c.value,
            )
            .where(monitoring_state.c.key.like(f"{LAST_CYCLE_KEY}:%"))
            .order_by(monitoring_state.c.key)
        )
        return [
            {
                "tms_id": str(row["key"]).removeprefix(f"{LAST_CYCLE_KEY}:"),
                "updated_at": row["updated_at"],
                "cycle": dict(row["value"] or {}),
            }
            for row in result.mappings()
        ]
