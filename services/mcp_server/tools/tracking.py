"""Outils MCP du domaine suivi : position, historique, itinéraire."""

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

# Plafond de points rendus par lecture d'historique. Un trajet long peut porter
# plusieurs milliers de positions ; les renvoyer toutes à un LLM saturerait sa
# fenêtre de contexte sans rien lui apprendre de plus (§20 : éviter les outils
# qui rendent des volumes inutiles).
MAX_HISTORY_POINTS = 200


def register(mcp: FastMCP, context: McpContext) -> None:
    @mcp.tool
    async def get_tms_time(tms_id: str) -> dict[str, Any]:
        """Heure courante du TMS, et état de son simulateur s'il y en a un.

        À utiliser comme référence de temps pour tout raisonnement sur les
        retards : l'horloge du TMS peut être accélérée ou remise à zéro, et ne
        coïncide pas avec l'heure réelle.
        """
        try:
            connection = context.connection(tms_id)
            health = await connection.connector.health()
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_tms_time", exception)
        return success({"tms_time": health.get("time"), "simulation": health.get("simulation")})

    @mcp.tool
    async def get_current_position(tms_id: str, vehicle_id: str) -> dict[str, Any]:
        """Dernière position connue d'un véhicule, avec sa date et sa vitesse.

        `position` peut être `null` : un véhicule hors couverture n'envoie plus
        rien. Cela ne veut pas dire qu'il est arrêté.
        """
        try:
            connection = context.connection(tms_id)
            vehicle = await connection.connector.get_vehicle(vehicle_id)
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_current_position", exception)

        if vehicle is None:
            return success({"vehicle_id": vehicle_id, "position": None, "status": None})
        return success(
            {
                "vehicle_id": vehicle_id,
                "status": to_jsonable(
                    connection.mapper.vehicle_status(
                        connection.mapper.field(Entity.VEHICLE, "status", vehicle)
                    )
                ),
                "last_seen_at": connection.mapper.field(Entity.VEHICLE, "last_seen_at", vehicle),
                "position": to_jsonable(
                    connection.mapper.position_of(Entity.VEHICLE, "current_position", vehicle)
                ),
            }
        )

    @mcp.tool
    async def get_position_history(
        tms_id: str,
        vehicle_id: str,
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Historique des positions d'un véhicule, du plus ancien au plus récent.

        Sert à comprendre *comment* un véhicule s'est écarté ou s'est arrêté,
        là où la position courante ne dit que l'état final. `limit` est plafonné
        à 200 points : au-delà, le détail n'éclaire plus l'analyse.
        """
        try:
            connection = context.connection(tms_id)
            raw = await connection.connector.get_vehicle_positions(
                vehicle_id,
                since=parse_datetime(since),
                limit=min(max(limit, 1), MAX_HISTORY_POINTS),
            )
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_position_history", exception)

        positions = [
            point for point in (connection.mapper.position(item) for item in raw) if point is not None
        ]
        # Les points les plus récents sont les plus utiles : si l'historique
        # dépasse le plafond, c'est le début qui est tronqué, pas la fin.
        truncated = positions[-MAX_HISTORY_POINTS:]
        return success(
            {
                "vehicle_id": vehicle_id,
                "count": len(truncated),
                "truncated": len(positions) > len(truncated),
                "positions": to_jsonable(truncated),
            }
        )

    @mcp.tool
    async def get_route(tms_id: str, mission_id: str) -> dict[str, Any]:
        """Itinéraire planifié d'une mission : points de passage, distance, durée.

        `route` vaut `null` lorsque aucun itinéraire n'a été planifié — l'écart
        à l'itinéraire est alors incalculable, et toute conclusion sur une
        dérive est sans fondement.
        """
        try:
            connection = context.connection(tms_id)
            raw = await connection.connector.get_route(mission_id)
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_route", exception)
        return success({"mission_id": mission_id, "route": to_jsonable(connection.mapper.route(raw))})
