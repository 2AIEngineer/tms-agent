"""Résolution des destinataires de notification dans le TMS.

Les règles désignent un **rôle** (`ops_supervisor`), jamais une personne. C'est
ici que le rôle devient un numéro de téléphone ou une adresse, au moment de
l'envoi (F16).

Pourquoi au moment de l'envoi, et non une fois pour toutes : l'annuaire vit
dans le TMS. Un superviseur qui change de numéro, une astreinte qui tourne, un
départ — tout cela se reflète immédiatement, sans synchronisation ni copie
d'annuaire à maintenir de notre côté.
"""

import logging
from dataclasses import dataclass
from typing import Any

from shared.mapped_dto import NotificationChannel

from .connectors.base import TmsFailure
from .connectors.rest_api_tms import RestApiTmsConnector

logger = logging.getLogger(__name__)

# Adresse à utiliser selon le canal. Un appel et un SMS veulent un numéro, un
# e-mail une adresse ; l'in-app n'a besoin que de l'identifiant utilisateur.
CHANNEL_ADDRESS_FIELD: dict[NotificationChannel, str | None] = {
    NotificationChannel.CALL: "phone",
    NotificationChannel.SMS: "phone",
    NotificationChannel.EMAIL: "email",
    NotificationChannel.IN_APP: None,
}


@dataclass(frozen=True, slots=True)
class Recipient:
    """Destinataire résolu, prêt à être notifié."""

    user_id: str
    name: str
    role: str
    management_level: str
    channel: NotificationChannel
    address: str | None

    @property
    def reachable(self) -> bool:
        """Le canal demandé dispose-t-il d'une adresse utilisable ?

        Un superviseur sans numéro de téléphone ne peut pas recevoir de SMS.
        L'envoi sera marqué en échec avec sa raison, et non réessayé sans fin
        (F17) : c'est une correction à faire dans le TMS, pas un incident
        technique passager.
        """
        return CHANNEL_ADDRESS_FIELD[self.channel] is None or bool(self.address)

    @property
    def unreachable_reason(self) -> str | None:
        if self.reachable:
            return None
        field = CHANNEL_ADDRESS_FIELD[self.channel]
        return f"{self.role} ({self.user_id}) n'a pas de {field} renseigné dans le TMS"


class RecipientResolver:
    def __init__(self, connector: RestApiTmsConnector) -> None:
        self._connector = connector

    async def resolve_role(
        self,
        role: str,
        channel: NotificationChannel,
        *,
        mission: dict[str, Any] | None = None,
    ) -> list[Recipient]:
        """Destinataires d'un rôle pour un canal donné.

        Le rôle `driver` est un cas à part : il désigne le chauffeur **de cette
        mission**, pas tous les chauffeurs de la flotte. Le traiter comme les
        autres appellerait toute la flotte à chaque dérive.
        """
        if role == "driver":
            return await self._resolve_mission_driver(channel, mission)

        try:
            users = await self._connector.list_users_by_role(role)
        except TmsFailure as failure:
            logger.warning("Rôle '%s' non résolu : %s", role, failure.error.message)
            raise

        if not users:
            logger.warning("Aucun utilisateur porteur du rôle '%s' dans le TMS", role)
        return [self._to_recipient(user, channel) for user in users if user.get("id")]

    async def resolve_user(self, user_id: str, channel: NotificationChannel) -> Recipient | None:
        """Destinataire désigné nominativement par une règle."""
        user = await self._connector.get_user(user_id)
        if user is None:
            logger.warning("Utilisateur %s introuvable dans le TMS", user_id)
            return None
        return self._to_recipient(user, channel)

    async def _resolve_mission_driver(
        self,
        channel: NotificationChannel,
        mission: dict[str, Any] | None,
    ) -> list[Recipient]:
        driver_id = (mission or {}).get("driver_id")
        if not driver_id:
            logger.warning("Rôle 'driver' demandé sans mission : aucun destinataire")
            return []
        driver = await self._connector.get_driver(str(driver_id))
        if driver is None:
            logger.warning("Chauffeur %s introuvable dans le TMS", driver_id)
            return []
        return [self._to_recipient(driver, channel)]

    @staticmethod
    def _to_recipient(user: dict[str, Any], channel: NotificationChannel) -> Recipient:
        address_field = CHANNEL_ADDRESS_FIELD[channel]
        raw_address = user.get(address_field) if address_field else None
        return Recipient(
            user_id=str(user.get("id")),
            name=str(user.get("name") or user.get("id")),
            role=str(user.get("role") or "unknown"),
            management_level=str(user.get("management_level") or "M+0"),
            channel=channel,
            address=str(raw_address).strip() if raw_address else None,
        )
