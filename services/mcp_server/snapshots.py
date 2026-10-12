"""Assemblage des instantanés de mission, au-dessus du connecteur TMS.

Le TMS répartit l'état d'une mission sur plusieurs ressources. Ce service les
lit **en parallèle** et en fait un `MissionSnapshot`, ce qui est exactement
l'étape F2 du cycle de surveillance.

Deux règles gouvernent ce module :

  - **la mission est obligatoire, ses satellites ne le sont pas.** Si le détail
    de la mission est illisible, il n'y a rien à surveiller et l'échec remonte.
    Si l'écart ou l'ETA manquent, le snapshot est rendu marqué incomplet : les
    autres règles doivent continuer de s'appliquer (F4) ;
  - **l'heure de référence est celle du TMS**, lue une fois par cycle et
    propagée à tous les snapshots, pour que les règles temporelles d'un même
    cycle jugent toutes sur le même instant (F3).
"""

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shared.mapped_dto import MissionSnapshot, MissionStatus, ToolError, ToolErrorCode

from .connectors.base import TmsFailure
from .connectors.dialect import Entity
from .connectors.rest_api_tms import RestApiTmsConnector
from .mappers import PivotMapper

logger = logging.getLogger(__name__)

# Statuts des missions que le cycle surveille. `planned` est inclus : une
# mission qui devait démarrer et ne démarre pas est une anomalie à détecter.
WATCHED_STATUSES: tuple[str, ...] = (MissionStatus.PLANNED.value, MissionStatus.IN_PROGRESS.value)


@dataclass(slots=True)
class SnapshotBatch:
    """Résultat d'une récolte d'instantanés sur un cycle.

    Les échecs voyagent avec les succès au lieu d'être levés : un cycle doit
    pouvoir traiter les missions lisibles et signaler les autres dans son
    bilan, sans s'interrompre (F4).
    """

    observed_at: datetime
    snapshots: list[MissionSnapshot] = field(default_factory=list)
    failures: dict[str, ToolError] = field(default_factory=dict)

    @property
    def partial(self) -> list[MissionSnapshot]:
        return [snapshot for snapshot in self.snapshots if not snapshot.snapshot_complete]

    def summary(self) -> str:
        return (
            f"missions={len(self.snapshots)} partielles={len(self.partial)} "
            f"illisibles={len(self.failures)} tms_time={self.observed_at.isoformat()}"
        )


class SnapshotService:
    def __init__(
        self,
        connector: RestApiTmsConnector,
        mapper: PivotMapper | None = None,
        *,
        mission_concurrency: int = 6,
    ) -> None:
        self._connector = connector
        # Le mapper par défaut est construit depuis le dialecte du connecteur :
        # il est impossible d'en obtenir un qui traduise un autre TMS.
        self._mapper = mapper or PivotMapper(connector.dialect)
        self._mission_concurrency = mission_concurrency

    async def tms_time(self) -> datetime:
        """Heure courante du TMS, référence temporelle du cycle (F3)."""
        return await self._connector.tms_time()

    async def list_watched_missions(self) -> list[dict[str, Any]]:
        """Missions à surveiller, telles que le TMS les rend."""
        return await self._connector.list_missions(statuses=list(WATCHED_STATUSES))

    async def build(self, mission_id: str, *, observed_at: datetime) -> MissionSnapshot | None:
        """Assemble l'instantané d'une mission.

        Le détail de la mission est lu d'abord, car il donne les identifiants du
        véhicule et du chauffeur dont dépendent les lectures suivantes. Les
        quatre ressources restantes sont ensuite lues en parallèle : lire en
        série multiplierait la latence par quatre, sur un cycle de dix secondes.
        """
        mission = await self._connector.get_mission(mission_id)
        missing: list[str] = []

        # Lus par leur nom **chez nous** : le dialecte sait où ce TMS les range.
        vehicle_id = self._mapper.field(Entity.MISSION, "vehicle_id", mission)
        driver_id = self._mapper.field(Entity.MISSION, "driver_id", mission)

        results = await asyncio.gather(
            self._optional("deviation", self._connector.get_deviation(mission_id), missing),
            self._optional("eta", self._connector.get_eta(mission_id), missing),
            self._optional("route", self._connector.get_route(mission_id), missing),
            self._optional(
                "vehicle",
                self._connector.get_vehicle(str(vehicle_id)) if vehicle_id else _none(),
                missing,
            ),
            self._optional(
                "driver",
                self._connector.get_driver(str(driver_id)) if driver_id else _none(),
                missing,
            ),
        )
        deviation, eta, route, vehicle, driver = results

        # Le détail de mission du TMS de référence porte déjà la chronologie
        # complète : s'en servir épargne un appel par mission et par cycle, ce
        # qui compte face au quota du TMS. Repli sur la ressource dédiée pour
        # un TMS qui ne les inclurait pas.
        events = self._mapper.embedded_events(mission)
        if events is None:
            events = await self._optional("events", self._connector.get_mission_events(mission_id), missing)

        return self._mapper.mission_snapshot(
            mission,
            observed_at=observed_at,
            events=events if isinstance(events, list) else None,
            vehicle=vehicle,
            driver=driver,
            deviation=deviation,
            eta=eta,
            route=route,
            missing_parts=tuple(missing),
        )

    async def build_many(
        self,
        mission_ids: list[str],
        *,
        observed_at: datetime,
    ) -> SnapshotBatch:
        """Assemble les instantanés de plusieurs missions, en parallèle borné.

        Le parallélisme est plafonné par `mission_concurrency` (F2) : sans lui,
        deux cents missions ouvriraient deux cents séries d'appels simultanées
        et le TMS se verrait infliger une rafale qu'aucun quota ne lisserait.
        """
        batch = SnapshotBatch(observed_at=observed_at)
        semaphore = asyncio.Semaphore(self._mission_concurrency)

        async def one(mission_id: str) -> None:
            async with semaphore:
                try:
                    snapshot = await self.build(mission_id, observed_at=observed_at)
                except TmsFailure as failure:
                    # Mission illisible : signalée dans le bilan, le cycle
                    # continue sur les autres (F4).
                    batch.failures[mission_id] = failure.error
                    return
                if snapshot is None:
                    batch.failures[mission_id] = ToolError.of(
                        ToolErrorCode.PARTIAL_DATA,
                        "Mission inexploitable : identifiant ou statut non reconnu.",
                    )
                    return
                batch.snapshots.append(snapshot)

        await asyncio.gather(*(one(mission_id) for mission_id in mission_ids))
        batch.snapshots.sort(key=lambda snapshot: snapshot.mission_id)
        return batch

    async def collect_cycle(self) -> SnapshotBatch:
        """Récolte complète d'un cycle : heure TMS, missions surveillées, instantanés."""
        observed_at = await self.tms_time()
        missions = await self.list_watched_missions()
        watched, unrecognized = self._partition(missions, self._mapper)

        batch = await self.build_many(watched, observed_at=observed_at)
        for mission_id, status in unrecognized:
            batch.failures[mission_id] = ToolError.of(
                ToolErrorCode.PARTIAL_DATA,
                f"statut de mission non reconnu : {status!r}",
                {"mission_id": mission_id},
            )
        return batch

    @staticmethod
    def _partition(
        missions: list[dict[str, Any]],
        mapper: PivotMapper,
    ) -> tuple[list[str], list[tuple[str, Any]]]:
        """Trie les missions rendues par le TMS en « à surveiller » et « statut inconnu ».

        Le filtre est **réappliqué côté client**, alors qu'il a déjà été demandé
        dans la requête. Ce n'est pas une redondance inutile : un TMS tiers qui
        ne gère pas le paramètre `status` peut l'ignorer et rendre toute sa
        base. Sans ce second tri, le cycle lirait l'intégralité des missions
        historiques à chaque passage.

        Les trois cas sont traités différemment, et c'est le point :
          - statut surveillé  -> instantané assemblé ;
          - statut connu mais hors périmètre (terminée, annulée) -> ignorée
            silencieusement, nous ne l'avions pas demandée ;
          - statut non reconnu -> **signalée**, car nous ne pouvons pas savoir
            si elle aurait dû être surveillée. La taire laisserait croire la
            flotte couverte alors qu'une mission échappe au suivi.
        """
        # La comparaison porte sur nos valeurs canoniques, après traduction par
        # le dialecte : un TMS qui dit `RUNNING` doit être reconnu comme en cours.
        known = {status.value for status in MissionStatus}
        watched: list[str] = []
        unrecognized: list[tuple[str, Any]] = []

        for mission in missions:
            mission_id = mapper.field(Entity.MISSION, "id", mission)
            if not mission_id:
                continue
            status = mapper.field(Entity.MISSION, "status", mission)
            canonical = mapper.dialect.canonical("mission_status", status)
            if canonical in WATCHED_STATUSES:
                watched.append(str(mission_id))
            elif canonical not in known:
                unrecognized.append((str(mission_id), status))

        return watched, unrecognized

    @staticmethod
    async def _optional[T](
        part: str,
        awaitable: Awaitable[T],
        missing: list[str],
    ) -> T | None:
        """Lit une partie facultative du snapshot, en notant son absence.

        Toute défaillance est absorbée et enregistrée : c'est ce qui transforme
        un appel TMS en échec en « snapshot partiel » plutôt qu'en mission
        perdue pour le cycle.
        """
        try:
            return await awaitable
        except TmsFailure as failure:
            logger.info("Partie '%s' indisponible : %s", part, failure.error.message)
            missing.append(part)
            return None


async def _none() -> None:
    """Attente neutre, pour garder `gather` homogène quand un identifiant manque."""
    return None
