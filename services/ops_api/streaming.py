"""Acheminement SSE des événements de l'assistant.

Ce module ne décide de rien : il met en forme ce que le service agent produit
et garde la connexion vivante. Tout ce qui relève des garde-fous, de la
persistance et de la validation humaine est en amont, dans le service — et
nulle part ici.

Deux détails de protocole méritent d'être écrits, parce que les ignorer donne
un flux qui « marche » en développement et casse derrière un mandataire.

**Le maintien en vie.** Un tour d'assistant peut rester silencieux une minute :
le modèle réfléchit, le TMS répond lentement. Sans trafic, un mandataire coupe
une connexion inactive et l'interface croit son flux vivant. D'où un
commentaire SSE périodique, qui ne porte aucune donnée et qu'un client ignore.

**L'attente ne doit pas annuler le travail en cours.** Attendre l'événement
suivant avec `wait_for` annulerait, au délai écoulé, l'étape que le générateur
avait commencée — une lecture du TMS perdue à chaque battement de cœur. La
tâche est donc conservée d'une attente à l'autre, et seulement abandonnée
lorsque le client s'en va.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from shared.mapped_dto import AssistantEvent, StreamError
from shared.utils.serialization import to_jsonable

logger = logging.getLogger(__name__)

# En-têtes d'un flux SSE. `X-Accel-Buffering` désactive la mise en tampon des
# mandataires : sans elle, les événements arrivent par paquets et le temps réel
# disparaît — précisément ce que ce flux existe pour apporter.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def format_event(name: str, payload: dict[str, Any]) -> str:
    """Met en forme un événement SSE nommé."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def format_assistant_event(event: AssistantEvent) -> str:
    """Met en forme un événement de l'assistant, nommé par son type.

    Le type est à la fois le nom de l'événement SSE et un champ de la charge
    utile : un client qui écoute par nom d'événement et un client qui lit tout
    sur un même canal s'en sortent l'un comme l'autre.
    """
    payload: dict[str, Any] = to_jsonable(event)
    return format_event(str(payload.get("type")), payload)


async def assistant_sse(
    events: AsyncIterator[AssistantEvent],
    *,
    keepalive_s: float,
) -> AsyncIterator[str]:
    """Achemine les événements de l'assistant, en gardant la connexion vivante.

    Une exception du service est rendue comme un événement `error` plutôt que
    comme une connexion coupée : une interface distingue mal une fermeture
    d'un silence, et ne saurait pas s'il faut réessayer.
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[AssistantEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=keepalive_s)
            if not done:
                # Commentaire SSE : aucun client ne le voit, tous les
                # mandataires le comptent comme du trafic.
                yield ": keepalive\n\n"
                continue

            task, pending = pending, None
            try:
                event = task.result()
            except StopAsyncIteration:
                return
            except asyncio.CancelledError:
                raise
            except Exception as exception:
                logger.exception("Flux de l'assistant interrompu")
                yield format_assistant_event(StreamError(message=f"Flux interrompu : {exception}"))
                return
            yield format_assistant_event(event)
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending
        # Ferme le générateur du service : c'est ce qui lui fait exécuter ses
        # propres `finally`, et donc achever ce qu'il avait à écrire.
        with contextlib.suppress(Exception):
            await iterator.aclose()  # type: ignore[attr-defined]
