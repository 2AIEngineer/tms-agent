"""Diffusion temps réel des alertes (Server-Sent Events).

## Pourquoi un sondage plutôt que `LISTEN`/`NOTIFY`

PostgreSQL sait pousser une notification, et ce serait séduisant. Deux raisons
de ne pas s'y fier ici :

  - **une notification perdue est perdue.** `NOTIFY` ne délivre qu'aux sessions
    connectées *à cet instant* : une reconnexion de l'interface, un redémarrage
    de l'API, un réseau qui hoquette, et l'alerte ne sera jamais rejouée. Un
    curseur en base, lui, rattrape tout ce qui s'est produit pendant la
    coupure — propriété décisive quand l'objet manqué est une alerte critique ;
  - **le producteur devrait collaborer.** Le worker de surveillance aurait à
    émettre le `NOTIFY` en plus de son insertion, ce qui ajoute un couplage
    pour un gain nul à notre échelle : quelques dizaines d'alertes par heure et
    une requête indexée toutes les deux secondes.

Le curseur porte sur `created_at`, l'horloge du **serveur**, et non sur
`detected_at`, qui est l'heure du TMS : en simulation accélérée, l'heure TMS
avance plus vite que le temps réel et un curseur posé dessus sauterait des
alertes.

Un battement de cœur est émis même sans alerte : sans lui, un mandataire coupe
une connexion inactive et l'interface croit son flux vivant.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from shared.utils.serialization import to_jsonable

from ..dependencies import Db, ReadAccess, Repository, State
from ..streaming import SSE_HEADERS, format_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["temps réel"])


@router.get("/alerts")
async def stream_alerts(
    request: Request,
    caller: ReadAccess,
    database: Db,
    repository: Repository,
    app_state: State,
) -> StreamingResponse:
    """Flux des alertes créées à partir de maintenant.

    Délibérément « à partir de maintenant » : l'historique se lit par
    `GET /alerts`, et rejouer une journée d'alertes à chaque ouverture
    d'onglet noierait l'opérateur sous des notifications déjà traitées.
    """
    settings = app_state.settings

    async def events() -> AsyncIterator[str]:
        cursor = datetime.now(UTC)
        last_beat = asyncio.get_running_loop().time()
        # Dit à l'interface que le flux est établi : sans cela, elle ne peut
        # pas distinguer « connecté, rien à signaler » de « connexion en cours ».
        yield _event("ready", {"since": cursor.isoformat()})

        while True:
            if await request.is_disconnected():
                return
            try:
                async with database.connection() as connection:
                    rows = await repository.new_alerts_since(connection, after=cursor, tms_ids=caller.tms_ids)
            except Exception:
                # Une base momentanément indisponible ne doit pas fermer le
                # flux : l'interface n'a aucun moyen de distinguer une
                # fermeture d'un calme plat, et ne se reconnecterait peut-être
                # pas. On signale et on retente au tour suivant.
                logger.exception("Lecture des nouvelles alertes en échec")
                yield _event("error", {"message": "lecture momentanément impossible"})
                rows = []

            for row in rows:
                cursor = max(cursor, row["created_at"])
                yield _event("alert", to_jsonable(row))

            now = asyncio.get_running_loop().time()
            if now - last_beat >= settings.sse_keepalive_s:
                last_beat = now
                yield _event("heartbeat", {"at": datetime.now(UTC).isoformat()})

            await asyncio.sleep(settings.sse_poll_interval_s)

    return StreamingResponse(
        _guarded(events()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _guarded(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Absorbe l'annulation : un onglet fermé n'est pas une erreur."""
    with contextlib.suppress(asyncio.CancelledError):
        async for chunk in stream:
            yield chunk


def _event(name: str, payload: dict[str, Any]) -> str:
    """Même mise en forme que le flux de l'assistant : une seule, partagée."""
    return format_event(name, payload)
