"""Outils MCP du domaine mission.

Trois capacités métier, pas une façade sur les tables du TMS : savoir ce qu'il
faut surveiller, obtenir l'état complet d'une mission, lire sa chronologie.
"""

from typing import Any

from fastmcp import FastMCP

from shared.utils.serialization import parse_datetime, to_jsonable

from ..connections import UnknownTms
from ..connectors.base import TmsFailure
from ..connectors.dialect import Entity
from ..context import (
    McpContext,
    failure_payload,
    success,
    unexpected_error,
    unknown_tms_payload,
)


def register(mcp: FastMCP, context: McpContext) -> None:
    @mcp.tool
    async def get_active_missions(tms_id: str) -> dict[str, Any]:
        """Liste les missions à surveiller (planifiées ou en cours).

        Rend aussi l'heure courante du TMS, qui est la référence temporelle de
        tout le système : les retards et les immobilisations se mesurent sur
        l'horloge du TMS, jamais sur celle du serveur.

        `tms_id` nomme le TMS à interroger. Obligatoire, même lorsqu'un seul
        est raccordé : il n'y a jamais de TMS implicite. `list_tms` donne la
        liste des TMS disponibles.
        """
        try:
            connection = context.connection(tms_id)
            observed_at = await connection.snapshots.tms_time()
            missions = await connection.snapshots.list_watched_missions()
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_active_missions", exception)

        field = connection.mapper.field
        return success(
            {
                "tms_id": connection.tms_id,
                "tms_time": observed_at.isoformat(),
                "count": len(missions),
                # Rendu dans **notre** vocabulaire, quel que soit celui du TMS :
                # c'est ce que l'agent et le monitoring attendent.
                "missions": [
                    {
                        "mission_id": field(Entity.MISSION, "id", mission),
                        "reference": field(Entity.MISSION, "reference", mission),
                        "status": to_jsonable(
                            connection.mapper.mission_status(field(Entity.MISSION, "status", mission))
                        ),
                        "vehicle_id": field(Entity.MISSION, "vehicle_id", mission),
                        "driver_id": field(Entity.MISSION, "driver_id", mission),
                        "planned_delivery_at": field(Entity.MISSION, "planned_delivery_at", mission),
                    }
                    for mission in missions
                ],
            }
        )

    @mcp.tool
    async def get_mission_snapshot(tms_id: str, mission_id: str) -> dict[str, Any]:
        """Donne l'état complet d'une mission à l'instant courant du TMS.

        Réunit en un seul appel la mission, son véhicule, son chauffeur, son
        écart à l'itinéraire, son ETA, son itinéraire et sa chronologie. C'est
        ce que lit le cycle de surveillance, et ce sur quoi les règles portent.

        Si une partie n'a pas pu être lue, elle est listée dans `missing_parts`
        et `snapshot_complete` vaut `false` : les données présentes restent
        exploitables, mais une conclusion ne doit pas être tirée d'une absence.

        `tms_id` nomme le TMS à interroger. Obligatoire, même lorsqu'un seul
        est raccordé : il n'y a jamais de TMS implicite. `list_tms` donne la
        liste des TMS disponibles.
        """
        try:
            connection = context.connection(tms_id)
            observed_at = await connection.snapshots.tms_time()
            snapshot = await connection.snapshots.build(mission_id, observed_at=observed_at)
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_mission_snapshot", exception)

        if snapshot is None:
            return success({"snapshot": None, "reason": "mission inexploitable (statut non reconnu)"})
        return success({"snapshot": to_jsonable(snapshot)})

    @mcp.tool
    async def get_mission_events(
        tms_id: str,
        mission_id: str,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Chronologie des événements d'une mission, du plus ancien au plus récent.

        `since` (date ISO 8601) ne rend que les événements postérieurs, pour
        suivre une mission sans relire tout son historique.

        Chaque événement porte `distance_to_target_km` lorsque c'est pertinent :
        la distance entre le lieu de l'événement et l'étape attendue.

        `tms_id` nomme le TMS à interroger. Obligatoire, même lorsqu'un seul
        est raccordé : il n'y a jamais de TMS implicite. `list_tms` donne la
        liste des TMS disponibles.
        """
        try:
            connection = context.connection(tms_id)
            mission = await connection.connector.get_mission(mission_id)
            raw_events = await connection.connector.get_mission_events(
                mission_id, since=parse_datetime(since)
            )
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_mission_events", exception)

        origin, destination = connection.mapper.mission_places(mission)
        events = connection.mapper.events(
            raw_events, mission_id=mission_id, origin=origin, destination=destination
        )
        return success({"count": len(events), "events": to_jsonable(events)})
