"""Routes des missions : une façade sur le serveur MCP, pas sur le TMS.

L'API d'exploitation ne connaît ni l'URL du TMS, ni ses endpoints, ni son
modèle JSON — le serveur MCP est l'unique frontière, et cette règle vaut pour
l'API comme pour le reste du système. Ces routes existent parce qu'un frontend
ne parle pas le protocole MCP, et parce qu'il ne doit pas détenir de jeton du
TMS.
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from ..dependencies import AppState, ReadAccess, State
from ..mcp import OpsMcp

router = APIRouter(prefix="/missions", tags=["missions"])


def _mcp(app_state: AppState) -> OpsMcp:
    if app_state.mcp is None:
        raise HTTPException(
            status_code=503,
            detail="Serveur MCP injoignable : les lectures du TMS sont momentanément indisponibles.",
        )
    return app_state.mcp


@router.get("")
async def active_missions(caller: ReadAccess, app_state: State) -> dict[str, Any]:
    """Missions à surveiller sur les TMS du périmètre de l'appelant."""
    return await _mcp(app_state).active_missions(caller.tms_ids)


@router.get("/{tms_id}/{mission_id}")
async def mission_snapshot(
    tms_id: str,
    mission_id: str,
    caller: ReadAccess,
    app_state: State,
) -> dict[str, Any]:
    """État complet d'une mission, dans notre vocabulaire."""
    if caller.tms_ids and tms_id not in caller.tms_ids:
        raise HTTPException(status_code=404, detail="Mission introuvable.")
    return await _mcp(app_state).mission_snapshot(tms_id, mission_id)
