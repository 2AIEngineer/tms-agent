"""Routes des alertes : ce que l'agent de suivi consulte toute la journée."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from shared.mapped_dto import AlertStatus, Severity
from shared.utils.serialization import to_jsonable

from ..dependencies import Db, ReadAccess, Repository

router = APIRouter(prefix="/alerts", tags=["alertes"])


@router.get("")
async def list_alerts(
    caller: ReadAccess,
    database: Db,
    repository: Repository,
    status: Annotated[AlertStatus | None, Query()] = None,
    min_severity: Annotated[Severity | None, Query()] = None,
    mission_id: Annotated[str | None, Query()] = None,
    tms_id: Annotated[list[str] | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Alertes filtrées, les plus graves et les plus récentes d'abord.

    Le périmètre de TMS de l'appelant **prime** sur le filtre demandé : un
    opérateur ne peut pas élargir sa vue en passant un `tms_id` auquel il n'a
    pas accès.
    """
    scope = _scope(caller.tms_ids, tms_id)
    if scope is EMPTY_SCOPE:
        # Filtre entièrement hors du périmètre de l'appelant. Rendre une liste
        # vide, et non « aucun filtre » : c'est précisément la confusion qui
        # ferait tout voir à un opérateur restreint.
        return {"count": 0, "total": 0, "alerts": []}
    async with database.connection() as connection:
        rows = await repository.list_alerts(
            connection,
            tms_ids=scope,
            status=status,
            min_severity=min_severity,
            mission_id=mission_id,
            since=since,
            limit=limit,
            offset=offset,
        )
        total = await repository.count_alerts(
            connection,
            tms_ids=scope,
            status=status,
            min_severity=min_severity,
            mission_id=mission_id,
            since=since,
        )
    return {"count": len(rows), "total": total, "alerts": to_jsonable(rows)}


@router.get("/summary")
async def summary(caller: ReadAccess, database: Db, repository: Repository) -> dict[str, Any]:
    """Compte des alertes ouvertes, par TMS et par sévérité."""
    async with database.connection() as connection:
        rendered: dict[str, Any] = to_jsonable(await repository.summary(connection, tms_ids=caller.tms_ids))
    return rendered


@router.get("/{alert_id}")
async def alert_detail(
    alert_id: uuid.UUID,
    caller: ReadAccess,
    database: Db,
    repository: Repository,
) -> dict[str, Any]:
    """Une alerte, son analyse, ses notifications, ses appels et son audit.

    Un 404 est rendu aussi bien pour une alerte inexistante que pour une
    alerte hors du périmètre de l'appelant : distinguer les deux révélerait
    l'existence d'alertes qu'il n'a pas le droit de voir.
    """
    async with database.connection() as connection:
        detail = await repository.alert_detail(connection, alert_id, tms_ids=caller.tms_ids)
    if detail is None:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    rendered: dict[str, Any] = to_jsonable(detail)
    return rendered


# Résultat d'une intersection vide. Un marqueur explicite, parce qu'un tuple
# vide signifie déjà quelque chose dans ce code — « aucune restriction » — et
# que confondre les deux donnerait à un opérateur restreint la vue complète.
EMPTY_SCOPE: tuple[str, ...] = ("\x00aucun",)


def _scope(allowed: tuple[str, ...], requested: list[str] | None) -> tuple[str, ...]:
    """Intersection du périmètre de l'appelant et du filtre demandé.

    Un tuple vide en sortie voudrait dire « aucune restriction » pour le
    dépôt : on rend donc `EMPTY_SCOPE` quand l'intersection est vide, et
    l'appelant s'arrête là.
    """
    if not allowed:
        return tuple(requested or ())
    if not requested:
        return allowed
    intersection = tuple(tms for tms in requested if tms in allowed)
    return intersection or EMPTY_SCOPE
