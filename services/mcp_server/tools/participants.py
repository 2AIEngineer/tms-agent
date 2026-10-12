"""Outils MCP du domaine participants : chauffeur, véhicule, destinataires."""

from typing import Any

from fastmcp import FastMCP

from shared.mapped_dto import NotificationChannel
from shared.utils.serialization import to_jsonable

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
from ..recipients import Recipient


def register(mcp: FastMCP, context: McpContext) -> None:
    @mcp.tool
    async def get_driver(tms_id: str, driver_id: str) -> dict[str, Any]:
        """Chauffeur : nom, téléphone, e-mail, et mission en cours s'il y en a une."""
        try:
            connection = context.connection(tms_id)
            raw = await connection.connector.get_driver(driver_id)
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_driver", exception)

        if raw is None:
            return success({"driver": None})
        return success(
            {
                "driver": to_jsonable(connection.mapper.driver(raw)),
                "status": connection.mapper.field(Entity.DRIVER, "status", raw),
                "current_mission_id": connection.mapper.field(Entity.DRIVER, "current_mission_id", raw),
            }
        )

    @mcp.tool
    async def get_vehicle(tms_id: str, vehicle_id: str) -> dict[str, Any]:
        """Véhicule : immatriculation, statut, dernière position connue."""
        try:
            connection = context.connection(tms_id)
            raw = await connection.connector.get_vehicle(vehicle_id)
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("get_vehicle", exception)

        if raw is None:
            return success({"vehicle": None})
        return success(
            {
                "vehicle": to_jsonable(connection.mapper.vehicle(raw)),
                "current_mission_id": connection.mapper.field(Entity.VEHICLE, "current_mission_id", raw),
            }
        )

    @mcp.tool
    async def resolve_recipients(
        tms_id: str,
        channel: str,
        role: str | None = None,
        user_id: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        """Qui prévenir pour un rôle (ou une personne) et un canal, d'après le TMS.

        Les destinataires sont résolus au moment de l'envoi, jamais figés : un
        changement de numéro ou une rotation d'astreinte dans le TMS prend
        effet immédiatement.

        Renseigner soit `role`, soit `user_id`, selon ce que la règle a désigné.
        `role` vaut `driver`, `ops_agent`, `ops_supervisor`, `ops_manager` ou
        `director`. Pour `driver`, `mission_id` est obligatoire : il désigne le
        chauffeur **de cette mission**, et non tous les chauffeurs de la flotte.

        `channel` vaut `in_app`, `sms`, `email` ou `call`. Chaque destinataire
        porte `reachable` : un superviseur sans numéro ne peut pas recevoir de
        SMS, et l'envoi doit être signalé en échec plutôt que réessayé sans fin.
        """
        try:
            connection = context.connection(tms_id)
            resolved_channel = NotificationChannel(channel)
        except ValueError:
            return success(
                {
                    "recipients": [],
                    "reason": f"canal inconnu : {channel!r} "
                    f"(attendu : {', '.join(c.value for c in NotificationChannel)})",
                }
            )

        if bool(role) == bool(user_id):
            return success({"recipients": [], "reason": "renseigner soit 'role', soit 'user_id'"})

        mission: dict[str, Any] | None = None
        try:
            connection = context.connection(tms_id)
            if user_id:
                nominative = await connection.recipients.resolve_user(user_id, resolved_channel)
                recipients = [nominative] if nominative else []
                return _render(role, user_id, resolved_channel, recipients)
            if role == "driver":
                if not mission_id:
                    return success({"recipients": [], "reason": "le rôle 'driver' exige un mission_id"})
                mission = await connection.connector.get_mission(mission_id)
            # `role` est non nul ici : le contrôle d'exclusivité en tête de
            # fonction a déjà écarté le cas où ni l'un ni l'autre n'est fourni.
            recipients = await connection.recipients.resolve_role(
                role or "", resolved_channel, mission=mission
            )
        except UnknownTms as error:
            return unknown_tms_payload(error)
        except TmsFailure as failure:
            return failure_payload(failure)
        except Exception as exception:
            return unexpected_error("resolve_recipients", exception)

        return success(
            {
                "role": role,
                "channel": resolved_channel.value,
                "count": len(recipients),
                "recipients": [
                    {
                        "user_id": recipient.user_id,
                        "name": recipient.name,
                        "management_level": recipient.management_level,
                        "address": recipient.address,
                        "reachable": recipient.reachable,
                        "unreachable_reason": recipient.unreachable_reason,
                    }
                    for recipient in recipients
                ],
            }
        )


def _render(
    role: str | None,
    user_id: str | None,
    channel: NotificationChannel,
    recipients: list[Recipient],
) -> dict[str, Any]:
    return success(
        {
            "role": role,
            "user_id": user_id,
            "channel": channel.value,
            "count": len(recipients),
            "recipients": [
                {
                    "user_id": recipient.user_id,
                    "name": recipient.name,
                    "management_level": recipient.management_level,
                    "address": recipient.address,
                    "reachable": recipient.reachable,
                    "unreachable_reason": recipient.unreachable_reason,
                }
                for recipient in recipients
            ],
        }
    )
