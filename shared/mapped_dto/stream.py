"""Événements du flux temps réel de l'assistant.

Un opérateur qui pose une question attend parfois une minute : le modèle lit le
TMS, revient, relit. Sans retour visible, il croit l'interface figée et repose
sa question. Ces événements rendent le travail observable **pendant** qu'il se
fait, comme le fait un agent de codage qui montre ses lectures.

Trois choix méritent d'être dits.

**Ce sont des contrats, pas un format d'affichage.** Ils traversent une
frontière de service — le service agent les produit, l'API d'exploitation les
achemine, le frontend les rend — donc ils vivent ici, stricts et figés comme
les autres.

**Il n'y a pas d'événement « pensée » fabriqué.** Un modèle servi par une API
compatible OpenAI n'expose pas son raisonnement, sauf s'il émet
explicitement du `reasoning_content` — alors nous le relayons
(`reasoning_delta`). Inventer un commentaire plausible à sa place serait
mentir à l'opérateur sur ce que la machine fait vraiment. Ce qui tient lieu de
raisonnement visible, ce sont les **étapes** : quels outils, dans quel ordre,
avec quel résultat.

**L'identifiant de conversation part en premier** (`started`), avant tout
jeton. L'interface doit pouvoir rattacher ce qui arrive à un fil, et un
opérateur qui ferme son onglet au milieu doit retrouver la conversation.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .assistant import AssistantAnswer, AssistantMode, PendingAction
from .base import Pivot


class StreamEventType(StrEnum):
    STARTED = "started"
    ROUND_STARTED = "round_started"
    REASONING_DELTA = "reasoning_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TEXT_DELTA = "text_delta"
    PENDING_ACTION = "pending_action"
    DONE = "done"
    ERROR = "error"


class ToolRole(StrEnum):
    """Nature d'un outil, pour que l'interface ne les présente pas pareil.

    Une lecture est anodine ; une action engage. Les afficher de la même façon
    rendrait invisible la seule chose qui compte pour un opérateur : est-ce que
    quelque chose va se produire ?
    """

    READ = "read"
    ACTION = "action"


class StreamStarted(Pivot):
    type: Literal[StreamEventType.STARTED] = StreamEventType.STARTED
    conversation_id: uuid.UUID
    mode: AssistantMode
    allow_actions: bool
    started_at: datetime


class RoundStarted(Pivot):
    """Un tour de raisonnement commence.

    Le plafond est transmis : « tour 5 sur 6 » dit à l'opérateur que le modèle
    approche de sa limite, ce qu'un simple numéro ne dirait pas.
    """

    type: Literal[StreamEventType.ROUND_STARTED] = StreamEventType.ROUND_STARTED
    round: int
    max_rounds: int


class ReasoningDelta(Pivot):
    """Fragment de raisonnement, lorsque le modèle en expose un."""

    type: Literal[StreamEventType.REASONING_DELTA] = StreamEventType.REASONING_DELTA
    text: str


class ToolStarted(Pivot):
    type: Literal[StreamEventType.TOOL_STARTED] = StreamEventType.TOOL_STARTED
    call_id: str
    tool: str
    role: ToolRole
    # Arguments rendus lisibles, valeurs tronquées : l'interface les affiche
    # sous le nom de l'outil, comme un agent de codage montre ses commandes.
    arguments: dict[str, str] = Field(default_factory=dict)


class ToolCompleted(Pivot):
    type: Literal[StreamEventType.TOOL_COMPLETED] = StreamEventType.TOOL_COMPLETED
    call_id: str
    tool: str
    role: ToolRole
    ok: bool
    duration_ms: int
    # Début du résultat, déjà réduit par les réducteurs. Assez pour que
    # l'opérateur voie *qu'il s'est passé quelque chose de sensé*, pas assez
    # pour noyer l'interface.
    preview: str = ""


class TextDelta(Pivot):
    type: Literal[StreamEventType.TEXT_DELTA] = StreamEventType.TEXT_DELTA
    text: str


class PendingActionProposed(Pivot):
    """Une action attend la validation de l'opérateur.

    Émis dès l'enregistrement, et non à la fin du flux : l'interface peut
    afficher le bouton de validation pendant que le modèle finit sa phrase.
    """

    type: Literal[StreamEventType.PENDING_ACTION] = StreamEventType.PENDING_ACTION
    action: PendingAction


class StreamDone(Pivot):
    """Fin du flux, avec la réponse complète.

    La réponse entière est renvoyée bien qu'elle ait été diffusée en fragments :
    c'est ce qui permet à une interface de se recaler sans tenir de compteur,
    et à un client non interactif de n'écouter que cet événement.
    """

    type: Literal[StreamEventType.DONE] = StreamEventType.DONE
    answer: AssistantAnswer


class StreamError(Pivot):
    """Incident inattendu. Le flux s'arrête, la conversation reste lisible."""

    type: Literal[StreamEventType.ERROR] = StreamEventType.ERROR
    message: str
    conversation_id: uuid.UUID | None = None


AssistantEvent = Annotated[
    StreamStarted
    | RoundStarted
    | ReasoningDelta
    | ToolStarted
    | ToolCompleted
    | TextDelta
    | PendingActionProposed
    | StreamDone
    | StreamError,
    Field(discriminator="type"),
]

# Longueur maximale d'une valeur d'argument et d'un aperçu de résultat. Ce flux
# va vers un navigateur : y verser un instantané complet de mission à chaque
# lecture ferait de l'interface le goulot d'étranglement.
MAX_ARGUMENT_CHARS = 120
MAX_PREVIEW_CHARS = 400


def summarize_arguments(arguments: dict[str, object]) -> dict[str, str]:
    """Rend les arguments d'un outil affichables, sans les trahir."""
    rendered: dict[str, str] = {}
    for key, value in arguments.items():
        text = value if isinstance(value, str) else str(value)
        rendered[str(key)] = text if len(text) <= MAX_ARGUMENT_CHARS else text[:MAX_ARGUMENT_CHARS] + "…"
    return rendered


def preview(payload: str) -> str:
    """Début d'un résultat d'outil, pour l'affichage."""
    return payload if len(payload) <= MAX_PREVIEW_CHARS else payload[:MAX_PREVIEW_CHARS] + "…"
