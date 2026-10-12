"""Dépendances partagées par les outils MCP.

Le contexte détient le **registre des TMS raccordés**. Chaque connexion porte
son dialecte, son transport, son mapper et ses garde-fous ; les outils y
accèdent par identifiant.

**Aucun TMS n'est implicite**, même lorsqu'il n'y en a qu'un seul raccordé.
Tout appel nomme le TMS qu'il vise. Un appelant qui s'habituerait à ne pas le
nommer deviendrait un défaut le jour du second raccordement — et ce défaut
serait silencieux : il surveillerait un client et pas l'autre.
"""

import logging
from dataclasses import dataclass
from typing import Any

from shared.mapped_dto import ToolError, ToolErrorCode

from .config import McpSettings, get_settings
from .connections import (
    TmsConnection,
    TmsRegistry,
    UnknownTms,
    build_registry,
    load_specs,
    spec_from_env,
)
from .connectors.base import TmsFailure
from .connectors.http_tms import HttpTmsConnector
from .guardrails import TmsGuardrails

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class McpContext:
    settings: McpSettings
    registry: TmsRegistry

    # --- Construction ---------------------------------------------------------

    @classmethod
    def build(cls, settings: McpSettings | None = None) -> "McpContext":
        resolved = settings or get_settings()
        if resolved.tms_connections_file is not None:
            specs = load_specs(resolved.tms_connections_file)
        else:
            specs = [spec_from_env(resolved)]
        return cls(settings=resolved, registry=build_registry(specs))

    @classmethod
    def for_connector(
        cls,
        connector: HttpTmsConnector,
        *,
        settings: McpSettings | None = None,
        guardrails: TmsGuardrails | None = None,
        tms_id: str | None = None,
    ) -> "McpContext":
        """Contexte à connexion unique, bâti sur un connecteur déjà ouvert.

        Sert à brancher un transport simulé ou un connecteur dont le client
        HTTP vient de l'appelant.
        """
        resolved = settings or get_settings()
        connection = TmsConnection.from_parts(connector, tms_id=tms_id, guardrails=guardrails)
        return cls(settings=resolved, registry=TmsRegistry({connection.tms_id: connection}))

    @classmethod
    def for_connections(
        cls,
        connections: list[TmsConnection],
        *,
        settings: McpSettings | None = None,
    ) -> "McpContext":
        """Contexte multi-TMS, à partir de connexions déjà ouvertes."""
        return cls(
            settings=settings or get_settings(),
            registry=TmsRegistry({connection.tms_id: connection for connection in connections}),
        )

    # --- Accès ---------------------------------------------------------------

    def connection(self, tms_id: str) -> TmsConnection:
        return self.registry.get(tms_id)

    @property
    def connections(self) -> list[TmsConnection]:
        return self.registry.all()

    @property
    def tms_ids(self) -> list[str]:
        return sorted(self.registry.connections)

    async def aclose(self) -> None:
        await self.registry.aclose()


def error_payload(error: ToolError) -> dict[str, Any]:
    """Forme sous laquelle un échec est rendu à l'appelant d'un outil.

    L'erreur est **rendue comme donnée** et non levée : l'agent doit pouvoir
    lire `code` et `retryable` pour décider s'il réessaie ou s'il conclut. Une
    exception ne lui laisserait qu'un message en texte libre, dans lequel il
    confondrait « la mission n'existe pas » avec « je n'ai pas pu la lire ».
    """
    return {
        "ok": False,
        "error": {
            "code": error.code.value,
            "message": error.message,
            "retryable": error.retryable,
            "details": error.details or {},
        },
    }


def success(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, **payload}


def unknown_tms_payload(error: UnknownTms) -> dict[str, Any]:
    """TMS non raccordé : une erreur d'argument, pas un incident technique.

    Réessayer n'y changera rien, et il n'y a rien à conclure sur la mission.
    """
    return error_payload(
        ToolError.of(ToolErrorCode.INVALID_ARGUMENT, str(error.args[0] if error.args else error))
    )


def unexpected_error(tool: str, exception: Exception) -> dict[str, Any]:
    """Dernier filet : un défaut imprévu ne doit pas faire tomber le serveur MCP.

    Le serveur est la seule porte vers le TMS pour la surveillance comme pour
    l'agent. Une exception non rattrapée dans un outil priverait les deux.
    """
    logger.exception("Erreur inattendue dans l'outil %s", tool)
    return error_payload(
        ToolError.of(
            ToolErrorCode.TMS_UNAVAILABLE,
            f"Erreur interne du serveur MCP dans '{tool}' : {exception}",
            {"tool": tool},
        )
    )


def failure_payload(failure: TmsFailure) -> dict[str, Any]:
    return error_payload(failure.error)
