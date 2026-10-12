"""`call_driver` exposé au modèle : un outil **local**, pas un outil MCP.

Le serveur MCP est l'unique frontière avec le TMS, et le TMS est en lecture
seule : un appel téléphonique n'est donc pas une opération TMS. Sa trace
appartient au domaine de l'agent, et faire écrire le serveur MCP dans la base
de l'orchestrateur mêlerait deux domaines que l'architecture sépare. L'outil
vit donc dans le service agent, à côté de `submit_assessment`.

**Le modèle ne fournit que le motif.** Ni le numéro, ni l'identifiant du
chauffeur, ni la mission : tout cela est lu dans le TMS à partir de l'enquête
en cours. Un modèle qui pourrait nommer son destinataire pourrait appeler le
chauffeur d'une autre mission, et le garde-fou de quota porterait sur la
mauvaise personne.
"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.mapped_dto import InvestigationRequest, MissionSnapshot
from shared.utils.db import Database

from ..integrations.mcp_client import AgentMcpClient
from ..investigator.submission import compact_schema
from .driver_call import BlockedReason, CallRequest, CallStatus, DriverCallService

logger = logging.getLogger(__name__)

CALL_TOOL_NAME = "call_driver"


class CallDriverArguments(BaseModel):
    """Le seul paramètre laissé au modèle : pourquoi il appelle."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        description=(
            "Pourquoi appeler et ce qui est demandé au chauffeur, en une phrase "
            "complète. Lu par le chauffeur et conservé pour audit."
        ),
    )


def call_driver_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": CALL_TOOL_NAME,
            "description": (
                "Appelle le chauffeur de la mission enquêtée. À n'employer que si "
                "aucune lecture ne peut répondre à la question. Un refus (quota, "
                "appel déjà passé) est rendu comme résultat : ne pas réessayer."
            ),
            "parameters": compact_schema(CallDriverArguments.model_json_schema()),
        },
    }


class DriverCallTool:
    """Fait le lien entre l'appel d'outil du modèle et le service d'appel.

    Résout le chauffeur et l'heure TMS depuis l'instantané de la mission : un
    seul appel MCP donne les deux, et l'heure ainsi obtenue est celle du TMS,
    comme pour les cooldowns d'alerte (F3).
    """

    name = CALL_TOOL_NAME

    def __init__(
        self,
        service: DriverCallService,
        database: Database,
        mcp: AgentMcpClient,
    ) -> None:
        self._service = service
        self._database = database
        self._mcp = mcp

    @staticmethod
    def definition() -> dict[str, Any]:
        return call_driver_tool_definition()

    async def invoke(self, request: InvestigationRequest, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = CallDriverArguments.model_validate(arguments)
        except ValidationError as error:
            first = error.errors()[0]
            return {"ok": False, "error": {"message": f"Argument invalide : {first['msg']}"}}

        snapshot = await self._snapshot(request)
        if snapshot is None or snapshot.driver is None:
            # Pas un échec de l'enquête : la mission a pu se terminer. Le
            # modèle doit pouvoir conclure sans appel.
            return {
                "ok": True,
                "status": CallStatus.BLOCKED.value,
                "placed": False,
                "blocked_reason": BlockedReason.NO_PHONE.value,
                "detail": "Chauffeur non identifiable sur cette mission au moment de l'enquête.",
                "call_id": None,
            }

        call = CallRequest(
            tms_id=request.tms_id,
            mission_id=request.mission_id,
            driver_id=snapshot.driver.id,
            reason=parsed.reason.strip(),
            tms_now=snapshot.observed_at,
            severity=request.alert.severity,
            requested_by="investigator",
            alert_id=request.alert_id,
            investigation_id=request.investigation_id,
            phone=snapshot.driver.phone,
        )
        async with self._database.transaction() as connection:
            outcome = await self._service.place(connection, call)

        payload = outcome.as_payload()
        if outcome.placed:
            # Aucun fournisseur de téléphonie n'est raccordé en développement :
            # l'appel est tracé mais ne rapporte aucune déclaration. Le dire,
            # plutôt que laisser le modèle inventer ce que le chauffeur a
            # répondu.
            payload["driver_statement"] = None
            payload["note"] = (
                "Appel enregistré et remis au canal d'appel. Aucune déclaration "
                "du chauffeur n'est disponible : renseigner `driver_call` avec "
                "`attempted: true` et `reached: false`."
            )
        return payload

    async def _snapshot(self, request: InvestigationRequest) -> MissionSnapshot | None:
        result = await self._mcp.call(
            "get_mission_snapshot",
            {"tms_id": request.tms_id, "mission_id": request.mission_id},
        )
        raw = result.payload.get("snapshot") if result.ok else None
        if not isinstance(raw, dict):
            return None
        try:
            return MissionSnapshot.model_validate(raw)
        except Exception:
            logger.warning("Instantané non conforme : appel au chauffeur impossible")
            return None
