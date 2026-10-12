"""Accès au serveur MCP depuis l'API d'exploitation.

Le frontend ne parle pas le protocole MCP et ne doit détenir aucun jeton du
TMS : ces lectures passent donc par l'API. Elle n'en profite pas pour inventer
un second chemin vers le TMS — elle appelle les mêmes outils que le worker et
l'agent.

Le périmètre est appliqué **ici** plutôt que laissé aux routes : une route qui
oublierait de filtrer exposerait les missions d'un TMS auquel l'opérateur n'a
pas accès.
"""

import logging
from typing import Any

from services.agents.integrations.mcp_client import AgentMcpClient

logger = logging.getLogger(__name__)

# Outils nécessaires à l'interface. Liste blanche, comme pour l'agent : l'API
# ne doit pas devenir une passerelle générique vers le serveur MCP.
OPS_TOOLS: frozenset[str] = frozenset(
    {
        "list_tms",
        "get_tms_time",
        "get_active_missions",
        "get_mission_snapshot",
        "get_mission_events",
        "get_position_history",
        "get_route",
    }
)


class OpsMcp:
    """Lectures du TMS pour l'interface, périmètre appliqué."""

    def __init__(self, client: AgentMcpClient) -> None:
        self._client = client

    async def connected_tms(self) -> list[str]:
        """Identifiants des TMS raccordés, tels que `list_tms` les nomme.

        La clé est `tms_id` : c'est le vocabulaire qu'exigent tous les autres
        outils. Un `entry["id"]` eût été plausible et faux — ce que l'API a
        d'ailleurs fait un temps, en interrogeant un TMS nommé « None ».
        """
        result = await self._client.call("list_tms", {})
        entries = result.payload.get("tms") or []
        return [str(entry["tms_id"]) for entry in entries if isinstance(entry, dict) and entry.get("tms_id")]

    async def active_missions(self, scope: tuple[str, ...]) -> dict[str, Any]:
        """Missions à surveiller, pour chaque TMS du périmètre.

        Un TMS en panne ne prive pas l'interface des autres : son échec est
        rapporté à côté des missions lues, et non à leur place.
        """
        targets = list(scope) or await self.connected_tms()
        missions: list[dict[str, Any]] = []
        clocks: dict[str, str] = {}
        failures: dict[str, str] = {}
        for tms_id in targets:
            result = await self._client.call("get_active_missions", {"tms_id": tms_id})
            if not result.ok:
                failures[tms_id] = str(result.payload.get("error", {}).get("message", "illisible"))
                continue
            clocks[tms_id] = str(result.payload.get("tms_time", ""))
            for mission in result.payload.get("missions") or []:
                if isinstance(mission, dict):
                    missions.append({**mission, "tms_id": tms_id})
        return {
            "count": len(missions),
            "missions": missions,
            "tms_time": clocks,
            "unreadable_tms": failures,
        }

    async def mission_snapshot(self, tms_id: str, mission_id: str) -> dict[str, Any]:
        result = await self._client.call("get_mission_snapshot", {"tms_id": tms_id, "mission_id": mission_id})
        return result.payload

    async def mission_events(self, tms_id: str, mission_id: str) -> dict[str, Any]:
        result = await self._client.call("get_mission_events", {"tms_id": tms_id, "mission_id": mission_id})
        return result.payload
