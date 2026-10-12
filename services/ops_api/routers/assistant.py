"""Routes de l'assistant opérateur (F24 à F26).

Deux points de correction tiennent dans cette couche :

  - **l'identité de l'opérateur vient du jeton, jamais du corps de la requête.**
    La laisser passer dans la charge utile permettrait de poser une question au
    nom d'un collègue, et d'agir en son nom dans le journal d'audit ;
  - **le droit d'agir est vérifié deux fois** : par le rôle (permission `act`)
    et par la demande explicite de l'opérateur (`allow_actions`). Les deux sont
    nécessaires — un rôle suffisant ne vaut pas consentement, et un
    consentement ne crée pas un rôle.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from services.agents.assistant.repository import ConversationRepository
from services.agents.config import get_settings as get_agent_settings
from shared.mapped_dto import ActionDecision, AssistantMode, AssistantRequest
from shared.utils.serialization import to_jsonable

from ..auth import Permission, Principal
from ..dependencies import ActAccess, Assistant, Db, ReadAccess, State
from ..streaming import SSE_HEADERS, assistant_sse

router = APIRouter(prefix="/assistant", tags=["assistant"])


class AskPayload(BaseModel):
    """Corps d'une demande à l'assistant.

    Aucun champ d'identité : l'opérateur est celui du jeton.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    mode: AssistantMode = AssistantMode.ASK
    conversation_id: uuid.UUID | None = None
    mission_id: str | None = None
    alert_id: uuid.UUID | None = None
    # Demande explicite d'agir, pour cette requête seulement (F26).
    allow_actions: bool = False


@router.post("")
async def ask(
    caller: ReadAccess,
    assistant: Assistant,
    app_state: State,
    payload: Annotated[AskPayload, Body()],
) -> dict[str, Any]:
    """Pose une question, poursuit une conversation ou demande un rapport."""
    _require_action_rights(caller, payload)
    answer = await assistant.respond(await _request_from(caller, payload, app_state))
    rendered: dict[str, Any] = to_jsonable(answer)
    return rendered


def _require_action_rights(caller: Principal, payload: "AskPayload") -> None:
    """Le rôle dit *qui peut agir*, la validation dira *quoi exactement*.

    Les deux sont nécessaires, et ce contrôle vient en premier : préparer un
    geste pour quelqu'un qui n'a pas le droit de l'exécuter serait une fausse
    promesse.
    """
    if payload.allow_actions and not caller.may(Permission.ACT):
        # Dire franchement que le rôle ne suffit pas : l'opérateur doit savoir
        # qu'il faut demander à son administrateur, non reformuler sa phrase.
        raise HTTPException(
            status_code=403,
            detail=(
                "Votre rôle ne permet pas d'agir (clôture, appel, décision). "
                "La question peut être posée sans autorisation d'agir."
            ),
        )


async def _request_from(
    caller: Principal,
    payload: "AskPayload",
    app_state: Any,
) -> AssistantRequest:
    """Compose la demande du service. L'identité vient du jeton, jamais du corps."""
    return AssistantRequest(
        operator_id=caller.operator_id,
        message=payload.message,
        mode=payload.mode,
        conversation_id=payload.conversation_id,
        tms_ids=await _scope(caller.tms_ids, app_state),
        allow_actions=payload.allow_actions,
        mission_id=payload.mission_id,
        alert_id=payload.alert_id,
    )


async def _scope(allowed: tuple[str, ...], app_state: Any) -> tuple[str, ...]:
    """TMS que l'assistant doit interroger.

    Un opérateur sans périmètre déclaré voit tous les TMS raccordés — et il
    faut alors les **nommer**, plutôt que de laisser le modèle les deviner.
    Mesuré : interrogé sans périmètre, le modèle local a inventé un
    `tms_id: "tms1"` au lieu d'appeler `list_tms`, et la lecture a échoué. Les
    énoncer coûte un appel au démarrage de la requête et supprime la question.
    """
    if allowed:
        return allowed
    if app_state.mcp is None:
        return ()
    try:
        return tuple(await app_state.mcp.connected_tms())
    except Exception:
        # Un échec ici ne doit pas priver l'opérateur de sa réponse : le
        # modèle dispose de `list_tms` pour se débrouiller.
        return ()


@router.post("/stream")
async def ask_stream(
    caller: ReadAccess,
    assistant: Assistant,
    app_state: State,
    payload: Annotated[AskPayload, Body()],
) -> StreamingResponse:
    """Même demande que `POST /assistant`, diffusée au fil de l'eau (SSE).

    Les événements disent ce qui se passe pendant que cela se passe : tour de
    raisonnement, outil appelé et son résultat, action soumise à validation,
    fragments de la réponse, puis la réponse complète.

    C'est **le même code** qui travaille derrière les deux routes : celle-ci
    écoute le flux, l'autre le consomme et n'en rend que la fin. Les
    garde-fous, l'écriture en base et la validation humaine sont identiques,
    parce qu'ils ne sont écrits qu'une fois.

    À consommer avec `fetch` et un lecteur de flux : `EventSource` ne sait
    faire que des requêtes GET, et la question de l'opérateur voyage dans le
    corps.
    """
    _require_action_rights(caller, payload)
    events = assistant.stream(await _request_from(caller, payload, app_state))
    return StreamingResponse(
        assistant_sse(events, keepalive_s=app_state.settings.sse_keepalive_s),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/pending")
async def pending_actions(caller: ReadAccess, assistant: Assistant) -> dict[str, Any]:
    """Ce qui attend la validation de cet opérateur.

    La question qu'un agent de suivi pose en ouvrant son écran — « qu'attend-on
    de moi ? » — et qui ne tient pas dans la continuité d'une requête HTTP.
    D'où des lignes en base plutôt qu'un état suspendu en mémoire : la liste
    survit à un onglet fermé, à un changement de poste et à un redéploiement.
    """
    waiting = await assistant.waiting_actions(caller.operator_id)
    return {"count": len(waiting), "pending_actions": to_jsonable(waiting)}


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: uuid.UUID,
    caller: ActAccess,
    assistant: Assistant,
) -> dict[str, Any]:
    """Valide une action préparée, qui s'exécute **telle qu'elle a été montrée**.

    Les arguments viennent de la ligne enregistrée, pas du modèle : il ne
    repasse pas, et ne peut donc pas substituer une autre alerte à celle que
    l'opérateur a lue. Le rôle est revérifié ici — une proposition reçue il y a
    dix minutes ne vaut pas habilitation maintenant.
    """
    answer = await assistant.decide(
        action_id, operator_id=caller.operator_id, decision=ActionDecision.CONFIRM
    )
    rendered: dict[str, Any] = to_jsonable(answer)
    return rendered


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: uuid.UUID,
    caller: ActAccess,
    assistant: Assistant,
) -> dict[str, Any]:
    """Refuse une action préparée. Rien n'est exécuté, et le refus est tracé."""
    answer = await assistant.decide(action_id, operator_id=caller.operator_id, decision=ActionDecision.REJECT)
    rendered: dict[str, Any] = to_jsonable(answer)
    return rendered


@router.post("/actions/{action_id}/confirm/stream")
async def confirm_action_stream(
    action_id: uuid.UUID,
    caller: ActAccess,
    assistant: Assistant,
    app_state: State,
) -> StreamingResponse:
    """Valide une action, en diffusant l'exécution et ce qui la suit.

    L'opérateur voit l'action partir, son résultat revenir, et — s'il reste une
    étape à sa demande initiale — la suivante lui être proposée.
    """
    events = assistant.decide_stream(
        action_id, operator_id=caller.operator_id, decision=ActionDecision.CONFIRM
    )
    return StreamingResponse(
        assistant_sse(events, keepalive_s=app_state.settings.sse_keepalive_s),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post("/actions/{action_id}/reject/stream")
async def reject_action_stream(
    action_id: uuid.UUID,
    caller: ActAccess,
    assistant: Assistant,
    app_state: State,
) -> StreamingResponse:
    """Refuse une action. Diffusé pour que le fil de discussion reste homogène."""
    events = assistant.decide_stream(
        action_id, operator_id=caller.operator_id, decision=ActionDecision.REJECT
    )
    return StreamingResponse(
        assistant_sse(events, keepalive_s=app_state.settings.sse_keepalive_s),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get("/reports")
async def reports(
    caller: ReadAccess,
    database: Db,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Rapports de situation produits à heure fixe (F24).

    Ils ne sont **pas** notifiés : l'assistant n'envoie rien (F26), et un
    rapport poussé par SMS finirait par passer, dans l'esprit de ses
    destinataires, pour un substitut aux alertes. Il est produit, conservé, et
    lu ici.
    """
    # Les rapports planifiés appartiennent à un opérateur **technique** et non
    # à une personne : personne ne les demande, et tout le monde les lit. Le
    # nom vient de la configuration du service agent, qui les produit — deux
    # constantes séparées finiraient par diverger et la liste serait vide.
    operator = get_agent_settings().report_operator_id
    repository = ConversationRepository()
    async with database.connection() as connection:
        rows = await repository.recent(
            connection,
            operator,
            mode=AssistantMode.REPORT,
            limit=limit,
        )
        rendered = [
            {
                "conversation_id": str(row["id"]),
                "produced_at": row["updated_at"],
                "report": await repository.last_answer(connection, row["id"]),  # type: ignore[arg-type]
            }
            for row in rows
        ]
    return {"count": len(rendered), "reports": to_jsonable(rendered)}


@router.get("/me")
async def me(caller: ReadAccess) -> dict[str, Any]:
    """Qui est l'appelant et ce qu'il peut faire.

    L'interface en tire le seul affichage qui dépende du rôle : proposer ou non
    l'autorisation d'agir. Le serveur reste juge — masquer un bouton n'est pas
    un contrôle d'accès.
    """
    return {
        "operator_id": caller.operator_id,
        "name": caller.name,
        "roles": list(caller.roles),
        "tms_ids": list(caller.tms_ids),
        "can_read": caller.may(Permission.READ),
        "can_act": caller.may(Permission.ACT),
    }


@router.get("/conversations")
async def conversations(
    caller: ReadAccess,
    database: Db,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, Any]:
    """Conversations de l'appelant, la plus récemment active d'abord.

    Toujours filtrées sur l'identité du jeton : la liste d'un collègue n'est
    jamais atteignable, quel que soit le paramètre.
    """
    repository = ConversationRepository()
    async with database.connection() as connection:
        rows = await repository.recent(connection, caller.operator_id, limit=limit)
    items = [
        {
            "conversation_id": str(row["id"]),
            "title": row["title"],
            "mode": row["mode"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    return {"count": len(items), "conversations": to_jsonable(items)}


@router.get("/conversations/{conversation_id}")
async def conversation(
    conversation_id: uuid.UUID,
    caller: ReadAccess,
    assistant: Assistant,
) -> dict[str, Any]:
    """Historique d'une conversation (F25).

    Une conversation qui n'appartient pas à l'appelant rend un historique vide,
    jamais celui d'un collègue.
    """
    messages = await assistant.history(conversation_id, caller.operator_id)
    return {
        "conversation_id": str(conversation_id),
        "count": len(messages),
        "messages": to_jsonable(messages),
    }
