"""Contrat du connecteur TMS et traduction des échecs.

Le connecteur est le seul endroit du système qui parle HTTP au TMS. Tout ce qui
en sort est soit une structure JSON brute, soit un `TmsFailure` porteur d'un
code d'erreur du contrat pivot — jamais une exception `httpx`. Sans cette
frontière, un détail du client HTTP remonterait jusqu'au prompt de l'enquêteur.
"""

from typing import Any, Protocol

from shared.mapped_dto import ToolError, ToolErrorCode


class TmsFailure(Exception):
    """Échec d'un appel au TMS, déjà qualifié pour le reste du système.

    Porte un `ToolError` plutôt qu'un message : l'agent doit pouvoir distinguer
    « la mission n'existe pas » de « je n'ai pas réussi à la lire », et décider
    s'il réessaie (§21 de l'architecture).
    """

    def __init__(self, error: ToolError) -> None:
        super().__init__(f"{error.code.value}: {error.message}")
        self.error = error

    @classmethod
    def of(
        cls,
        code: ToolErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> "TmsFailure":
        return cls(ToolError.of(code, message, details))

    @property
    def retryable(self) -> bool:
        return self.error.retryable


def failure_from_status(status_code: int, resource: str, detail: str | None) -> TmsFailure:
    """Traduit un statut HTTP en code d'erreur du contrat pivot.

    La distinction 404 / 5xx est la plus importante de toute la frontière : la
    première est un fait métier, la seconde un incident technique. Les
    confondre conduirait l'enquêteur à conclure « la mission n'existe pas »
    devant un TMS simplement en panne.
    """
    message = detail or f"Le TMS a répondu {status_code} pour {resource}."

    if status_code == 404:
        return TmsFailure.of(ToolErrorCode.MISSION_NOT_FOUND, message, {"resource": resource})
    if status_code == 429:
        return TmsFailure.of(ToolErrorCode.TMS_RATE_LIMIT, message, {"resource": resource})
    if status_code in (400, 422):
        return TmsFailure.of(ToolErrorCode.INVALID_ARGUMENT, message, {"resource": resource})
    if status_code in (401, 403):
        return TmsFailure.of(ToolErrorCode.NOT_PERMITTED, message, {"resource": resource})
    if status_code >= 500:
        return TmsFailure.of(ToolErrorCode.TMS_UNAVAILABLE, message, {"resource": resource})
    return TmsFailure.of(
        ToolErrorCode.TMS_UNAVAILABLE,
        message,
        {"resource": resource, "status_code": status_code},
    )


class TmsConnector(Protocol):
    """Ce que tout connecteur TMS doit savoir faire.

    Un protocole et non une classe de base : brancher un nouveau TMS consiste à
    écrire une classe qui répond à ces appels, sans hériter d'une hiérarchie
    dont elle n'aurait que faire. Tous les appels sont des lectures — le système
    ne modifie jamais le TMS.
    """

    async def tms_time(self) -> Any: ...
    async def list_missions(self, *, statuses: list[str] | None = None) -> list[dict[str, Any]]: ...
    async def get_mission(self, mission_id: str) -> dict[str, Any]: ...
    async def get_mission_events(
        self, mission_id: str, *, since: Any | None = None
    ) -> list[dict[str, Any]]: ...
    async def get_route(self, mission_id: str) -> dict[str, Any] | None: ...
    async def get_deviation(self, mission_id: str) -> dict[str, Any] | None: ...
    async def get_eta(self, mission_id: str) -> dict[str, Any] | None: ...
    async def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None: ...
    async def get_driver(self, driver_id: str) -> dict[str, Any] | None: ...
    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...
    async def list_users_by_role(self, role: str) -> list[dict[str, Any]]: ...
    async def aclose(self) -> None: ...
