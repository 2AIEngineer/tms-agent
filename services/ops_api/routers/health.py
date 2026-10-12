"""Routes d'état : ce que l'exploitation regarde avant de chercher ailleurs."""

from typing import Any

from fastapi import APIRouter

from shared.utils.serialization import to_jsonable

from ..dependencies import Db, ReadAccess, Repository, State

router = APIRouter(tags=["état"])


@router.get("/health")
async def health(database: Db) -> dict[str, Any]:
    """Sonde de vivacité, sans authentification.

    Interrogée par la plateforme d'hébergement : exiger un jeton ferait
    redémarrer le conteneur en boucle. Elle ne révèle rien d'autre que la
    capacité à joindre la base.
    """
    try:
        async with database.connection() as connection:
            await connection.exec_driver_sql("select 1")
    except Exception as exception:
        return {"ok": False, "database": f"injoignable : {exception}"}
    return {"ok": True, "database": "ok"}


@router.get("/monitoring")
async def monitoring(
    caller: ReadAccess,
    database: Db,
    repository: Repository,
    app_state: State,
) -> dict[str, Any]:
    """Dernier cycle de surveillance par TMS, et file d'enquêtes (F27).

    La panne la plus grave de ce système est silencieuse : un worker arrêté ne
    produit aucune alerte, ce qui ressemble à une flotte sans anomalie. Cette
    route est ce qui rend la différence visible.
    """
    async with database.connection() as connection:
        cycles = await repository.monitoring_health(connection, tms_ids=caller.tms_ids)
        queue = await repository.investigation_queue(connection)
    return {
        "cycles": to_jsonable(cycles),
        "investigations": queue,
        "auth_disabled": app_state.settings.auth_disabled,
    }
