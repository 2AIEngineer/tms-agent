"""Outil MCP dédié au cycle de surveillance.

Un outil composite, et c'est délibéré (§20) : le cycle a besoin, à chaque
passage, de l'heure du TMS **et** de l'instantané de toutes les missions
surveillées, cohérents entre eux. Les obtenir par `get_active_missions` puis un
`get_mission_snapshot` par mission coûterait 1 + N allers-retours MCP et, pire,
relirait l'heure du TMS N fois : deux missions d'un même cycle seraient alors
jugées sur des instants différents, ce qui contredit F3.

Cet outil est fait pour le `monitoring_worker`, pas pour un LLM : il rend
volontairement un volume important. L'enquêteur, lui, utilise
`get_mission_snapshot` sur la seule mission qui l'intéresse.
"""

from typing import Any

from fastmcp import FastMCP

from shared.utils.serialization import to_jsonable

from ..connections import UnknownTms
from ..connectors.base import TmsFailure
from ..context import (
    McpContext,
    success,
    unexpected_error,
    unknown_tms_payload,
)

# Garde-fou : au-delà, le cycle doit découper lui-même. Évite qu'une flotte
# inattendue produise une réponse que personne ne pourra traiter en 10 s.
MAX_MISSIONS_PER_CYCLE = 500


def register(mcp: FastMCP, context: McpContext) -> None:
    @mcp.tool
    async def get_monitoring_cycle(
        tms_id: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Récolte complète d'un cycle de surveillance.

        Rend l'heure courante de chaque TMS et l'instantané de chacune de ses
        missions planifiées ou en cours. Au sein d'un même TMS, tous les
        instantanés sont rapportés au même instant.

        Sans `tms_id`, la récolte porte sur **tous** les TMS raccordés : un
        cycle doit couvrir toute la flotte, et obliger le worker à les énumérer
        reviendrait à lui faire connaître la configuration du serveur. Avec
        `tms_id`, la récolte se limite à ce TMS.

        - `tms_time` : une heure par TMS. Elles n'ont aucune raison de
          coïncider, et chaque instantané se juge sur celle de son TMS (F3).
        - `snapshots` : instantanés exploitables, chacun portant sa provenance
          dans `tms_id`. `snapshot_complete` et `missing_parts` signalent une
          partie absente, qui n'invalide pas le reste.
        - `failures` : missions illisibles, indexées `tms_id:mission_id` — deux
          TMS peuvent porter une mission du même identifiant. Un TMS entier
          injoignable apparaît sous `tms_id:*`, et n'empêche pas les autres
          d'être surveillés (F4).

        Destiné au worker de surveillance. Pour analyser une mission précise,
        préférer `get_mission_snapshot`.
        """
        try:
            connections = [context.connection(tms_id)] if tms_id is not None else context.connections
        except UnknownTms as error:
            return unknown_tms_payload(error)

        capped = min(limit, MAX_MISSIONS_PER_CYCLE) if limit else MAX_MISSIONS_PER_CYCLE
        snapshots: list[Any] = []
        failures: dict[str, dict[str, Any]] = {}
        requests: dict[str, Any] = {}
        clocks: dict[str, str] = {}

        for connection in connections:
            try:
                batch = await connection.snapshots.collect_cycle()
            except TmsFailure as failure:
                # Un TMS injoignable ne doit pas priver les autres de
                # surveillance : son échec voyage avec les succès.
                failures[f"{connection.tms_id}:*"] = {
                    "code": failure.error.code.value,
                    "message": failure.error.message,
                    "retryable": failure.error.retryable,
                }
                continue
            except Exception as exception:
                return unexpected_error("get_monitoring_cycle", exception)

            clocks[connection.tms_id] = batch.observed_at.isoformat()
            snapshots.extend(to_jsonable(snapshot) for snapshot in batch.snapshots)
            for mission_id, mission_error in batch.failures.items():
                failures[f"{connection.tms_id}:{mission_id}"] = {
                    "code": mission_error.code.value,
                    "message": mission_error.message,
                    "retryable": mission_error.retryable,
                }
            requests[connection.tms_id] = connection.guardrails.stats

        return success(
            {
                "tms_time": clocks,
                "count": len(snapshots[:capped]),
                "truncated": len(snapshots) > capped,
                "snapshots": snapshots[:capped],
                "failures": failures,
                "tms_requests": requests,
            }
        )
