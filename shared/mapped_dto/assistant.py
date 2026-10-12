"""Contrats de l'assistant opérateur (F24 à F26).

L'assistant est le second endroit où un LLM intervient, et son cadre diffère de
celui de l'enquêteur sur un point décisif : **il parle à un humain qui attend
une réponse**, là où l'enquêteur produit une structure lue par une interface.
Sa sortie est donc du texte — mais un texte accompagné de ce qui permet de le
vérifier : les sources consultées et les actions réellement exécutées.

Trois modes, parce qu'un opérateur ne demande pas la même chose selon le
moment (F24) :

  - `ask` — une question ponctuelle, sans suite ;
  - `chat` — une conversation, dont l'historique compte (F25) ;
  - `report` — un état de la situation, à heure fixe ou à la demande.

Le mode ne change pas les capacités : il change le prompt, la longueur attendue
et l'historique relu. Un même opérateur peut poursuivre en `chat` une
conversation ouverte par un `ask`.

**L'assistant n'émet jamais d'alerte et ne notifie personne** (F26). Ce n'est
pas une restriction de prudence : alerter est le travail des règles, et un LLM
qui pourrait notifier rendrait la détection non déterministe.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .base import Pivot


class AssistantMode(StrEnum):
    ASK = "ask"
    CHAT = "chat"
    REPORT = "report"


class MessageRole(StrEnum):
    """Rôle d'un message dans une conversation.

    `tool` est conservé comme les autres : une réponse qui s'appuie sur une
    lecture du TMS doit rester vérifiable après coup, et un historique qui
    n'aurait gardé que les tours de parole ne permettrait plus de savoir sur
    quoi l'assistant s'était appuyé.
    """

    OPERATOR = "operator"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AssistantStatus(StrEnum):
    """État d'un tour de conversation, tel que l'interface doit le rendre.

    `awaiting_confirmation` n'est pas une erreur : c'est le cas normal d'une
    demande d'action. L'assistant a préparé un geste précis et attend que
    l'opérateur le valide. Sans cet état, l'interface ne pourrait pas
    distinguer « voici ma réponse » de « j'attends votre accord ».
    """

    ANSWERED = "answered"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    UNAVAILABLE = "unavailable"


class PendingActionStatus(StrEnum):
    """Cycle de vie d'une action proposée.

    `executing` existe pour une raison précise : entre la validation et
    l'exécution, un processus peut mourir. Une action qui resterait `proposed`
    serait rejouable — deux appels au même chauffeur — et une action
    directement passée à `executed` mentirait si l'exécution a échoué.
    """

    PROPOSED = "proposed"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActionKind(StrEnum):
    """Les seules actions que l'assistant peut exécuter, sur demande explicite.

    Fermée à dessein. Clore une alerte, appeler un chauffeur, consigner une
    décision : trois gestes qu'un opérateur accomplirait lui-même, et rien de
    plus. Notifier et alerter n'y figurent pas (F26).
    """

    CLOSE_ALERT = "close_alert"
    CALL_DRIVER = "call_driver"
    LOG_DECISION = "log_decision"


class AssistantRequest(Pivot):
    """Ce qu'un opérateur demande.

    `conversation_id` absent ouvre une conversation ; fourni, il la poursuit —
    c'est ce qui rend l'historique utile après un redémarrage (F25).
    """

    operator_id: str = Field(min_length=1, description="Qui demande : repris dans l'audit (F27).")
    message: str = Field(min_length=1)
    mode: AssistantMode = AssistantMode.ASK
    conversation_id: uuid.UUID | None = None
    # TMS sur lesquels cet opérateur travaille. Vide signifie « tous ceux qui
    # sont raccordés » : l'assistant découvre alors la liste par `list_tms`.
    tms_ids: tuple[str, ...] = ()
    # Autorisation d'agir, et non simple préférence. L'opérateur doit l'avoir
    # demandé explicitement : sans ce drapeau, les outils d'action ne sont pas
    # même présentés au modèle, qui ne peut donc pas les appeler par zèle.
    allow_actions: bool = False
    mission_id: str | None = None
    alert_id: uuid.UUID | None = None


class PendingAction(Pivot):
    """Un geste préparé, en attente de la validation de l'opérateur.

    **Ce que l'opérateur valide est cette ligne**, pas une intention. Au
    moment de la validation, les arguments enregistrés ici sont exécutés tels
    quels : le modèle ne repasse pas, et ne peut donc pas substituer une autre
    alerte à celle qui a été montrée.

    `summary` est construit par le code, de façon déterministe, à partir des
    arguments et de la base. La prose du modèle est utile mais ne fait pas
    foi : l'opérateur doit lire ce qui va réellement s'exécuter.
    """

    id: uuid.UUID
    kind: ActionKind
    status: PendingActionStatus
    summary: str
    reason: str
    conversation_id: uuid.UUID
    arguments: dict[str, str] = Field(default_factory=dict)
    alert_id: uuid.UUID | None = None
    mission_id: str | None = None
    tms_id: str | None = None
    # Une autorisation ne vieillit pas bien : un accord donné il y a deux
    # heures ne dit rien de la situation actuelle de la mission.
    expires_at: datetime | None = None
    created_at: datetime | None = None


class ExecutedAction(Pivot):
    """Une action réellement exécutée, telle qu'elle est rendue à l'opérateur.

    `succeeded: false` est un cas normal : un quota d'appel atteint, une alerte
    déjà close. L'opérateur doit lire ce qui a échoué et pourquoi, plutôt que
    de supposer que sa demande a été suivie.
    """

    kind: ActionKind
    succeeded: bool
    detail: str = ""
    alert_id: uuid.UUID | None = None
    mission_id: str | None = None


class ConsultedSource(Pivot):
    """Une lecture sur laquelle la réponse s'appuie.

    Sans elle, une réponse d'assistant n'est qu'une affirmation. Avec elle, un
    opérateur peut aller vérifier — et c'est ce qui rend l'outil utilisable
    dans une exploitation.
    """

    tool: str
    summary: str = ""


class AssistantAnswer(Pivot):
    """La réponse rendue à l'opérateur."""

    conversation_id: uuid.UUID
    answer: str
    mode: AssistantMode
    status: AssistantStatus = AssistantStatus.ANSWERED
    sources: tuple[ConsultedSource, ...] = ()
    # Actions réellement exécutées pendant ce tour.
    actions: tuple[ExecutedAction, ...] = ()
    # Actions préparées et soumises à validation. Leur présence est ce qui fait
    # passer `status` à `awaiting_confirmation`.
    pending_actions: tuple[PendingAction, ...] = ()
    # Vrai lorsque la réponse a été coupée par la limite de jetons du modèle.
    # Mesuré en conditions réelles : sans ce drapeau, l'opérateur lit une phrase
    # interrompue au milieu et ne peut pas savoir s'il manque l'essentiel.
    truncated: bool = False
    # Vrai lorsque le modèle n'était pas joignable. La distinction compte pour
    # l'interface : « l'assistant est indisponible » n'est pas « l'assistant
    # n'a rien trouvé ».
    unavailable: bool = False
    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    answered_at: datetime | None = None


class ActionDecision(StrEnum):
    """Ce que l'opérateur répond à une action proposée."""

    CONFIRM = "confirm"
    REJECT = "reject"


class ConversationMessage(BaseModel):
    """Un message tel qu'il est conservé et relu.

    Modèle non figé : il sort d'une lecture en base, et `seq` lui est donné par
    la base elle-même.
    """

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str
    seq: int = 0
    tool_name: str | None = None
    created_at: datetime | None = None
