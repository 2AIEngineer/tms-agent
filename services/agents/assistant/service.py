"""Boucle de l'assistant : question, lectures, réponse (F24 à F26).

    message de l'opérateur
        -> historique relu en base               F25
        -> modèle
            -> lectures MCP (plusieurs tours, bornés)
            -> actions, si et seulement si autorisées   F26
        -> réponse en langage naturel + sources + actions

Deux workflows distincts, pas une abstraction commune. L'assistant et
l'enquêteur partagent le client MCP, le client d'inférence et les contrats —
mais pas leur logique : l'un répond à un humain et peut agir sur demande,
l'autre produit une structure et ne décide de rien. Les fondre derrière une
classe de base rendrait les deux plus difficiles à lire et à tester.

**L'historique est persisté quoi qu'il arrive.** Y compris lorsque le modèle
est indisponible : une question posée doit rester visible dans la conversation,
sinon l'opérateur croit avoir mal cliqué et la repose.
"""

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.mapped_dto import (
    ActionDecision,
    ActionKind,
    AssistantAnswer,
    AssistantEvent,
    AssistantMode,
    AssistantRequest,
    AssistantStatus,
    ConsultedSource,
    ConversationMessage,
    ExecutedAction,
    MessageRole,
    PendingAction,
    PendingActionProposed,
    ReasoningDelta,
    RoundStarted,
    StreamDone,
    StreamError,
    StreamStarted,
    TextDelta,
    ToolCompleted,
    ToolRole,
    ToolStarted,
    preview,
    summarize_arguments,
)
from shared.utils.db import Database

from ..integrations.budget import (
    DEFAULT_MAX_INPUT_TOKENS,
    TIGHTER_BUDGET_RATIO,
    ContextBudget,
)
from ..integrations.litellm_client import (
    LiteLlmClient,
    LlmCallFailed,
    LlmReply,
    ToolCall,
)
from ..integrations.mcp_client import MAX_TOOL_RESULT_CHARS, AgentMcpClient
from .actions import ActionContext, AssistantActions
from .pending import (
    DEFAULT_TTL,
    REQUIRES_CONFIRMATION,
    ClaimedAction,
    PendingActionRepository,
)
from .prompts import FINAL_ROUND_NUDGE, build_user_message, system_prompt
from .repository import DEFAULT_HISTORY_LIMIT, ConversationRepository

logger = logging.getLogger(__name__)

UNAVAILABLE_ANSWER = (
    "L'assistant est momentanément indisponible (modèle injoignable). "
    "La détection et les notifications, elles, continuent de fonctionner."
)

# Ajouté à une réponse coupée par la limite de jetons du modèle. Le dire est
# indispensable : une phrase interrompue au milieu laisse l'opérateur croire
# qu'il a tout lu.
TRUNCATION_NOTICE = (
    "\n\n[Réponse coupée par la limite de longueur du modèle : demandez le détail point par point.]"
)

NO_ANSWER = (
    "Je n'ai pas réussi à formuler une réponse à partir de ce que j'ai pu lire. "
    "Reformulez la question, ou consultez la mission directement."
)

# Réponses déterministes des décisions humaines. Déterministes à dessein : ce
# que l'opérateur lit après avoir cliqué ne doit dépendre d'aucun modèle.
REJECTED_ANSWER = "Action annulée à votre demande. Rien n'a été exécuté."
ALREADY_DECIDED = (
    "Cette action n'est plus en attente : elle a déjà été tranchée, a expiré, "
    "ou ne vous était pas adressée. Rien n'a été exécuté."
)


class AssistantFailed(Exception):
    """Le flux s'est interrompu sans réponse.

    Levée par `respond` seulement : un appelant qui écoute le flux voit
    l'événement `error` et décide lui-même quoi en faire.
    """


@dataclass(slots=True)
class _Turn:
    """Ce qu'un tour de conversation accumule, avant d'être persisté."""

    sources: list[ConsultedSource] = field(default_factory=list)
    actions: list[ExecutedAction] = field(default_factory=list)
    pending: list[PendingAction] = field(default_factory=list)
    transcript: list[ConversationMessage] = field(default_factory=list)
    rounds: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    # Issue du tour, renseignée par la boucle. Portée par le tour plutôt que
    # rendue, parce que la boucle est désormais un générateur : un `return` y
    # servirait à autre chose.
    answer: str = ""
    unavailable: bool = False


class AssistantService:
    """Répond à un opérateur, en lisant le TMS et, sur demande, en agissant."""

    def __init__(
        self,
        llm: LiteLlmClient,
        mcp: AgentMcpClient,
        actions: AssistantActions,
        *,
        database: Database,
        repository: ConversationRepository | None = None,
        pending: PendingActionRepository | None = None,
        max_tool_rounds: int = 6,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
        action_ttl: timedelta = DEFAULT_TTL,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    ) -> None:
        self._llm = llm
        self._mcp = mcp
        self._actions = actions
        self._database = database
        self._repository = repository or ConversationRepository()
        self._pending = pending or PendingActionRepository()
        self._max_rounds = max_tool_rounds
        self._history_limit = history_limit
        self._max_tool_result_chars = max_tool_result_chars
        self._action_ttl = action_ttl
        self._budget = ContextBudget(max_input_tokens)

    async def respond(self, request: AssistantRequest) -> AssistantAnswer:
        """Répond à l'opérateur, et rend la réponse complète.

        **Consomme le flux** plutôt que de refaire le travail. Deux chemins de
        code se mettraient tôt ou tard à divergerー l'un persistant les lectures
        d'outil, l'autre l'oubliant ; l'un respectant la validation humaine,
        l'autre non. Ici, diffuser ou ne pas diffuser ne change que ce que
        l'appelant écoute.
        """
        answer: AssistantAnswer | None = None
        async for event in self.stream(request):
            if isinstance(event, StreamDone):
                answer = event.answer
            elif isinstance(event, StreamError):
                raise AssistantFailed(event.message)
        if answer is None:  # pragma: no cover - le flux finit toujours par `done`
            raise AssistantFailed("Flux interrompu sans réponse.")
        return answer

    async def stream(self, request: AssistantRequest) -> AsyncIterator[AssistantEvent]:
        """Conduit un tour en diffusant ce qui se passe, au fil de l'eau.

        **L'implémentation de référence.** `respond` l'écoute ; l'API
        d'exploitation la relaie en SSE. Les garde-fous, la persistance et la
        validation humaine sont ici et nulle part ailleurs.

        L'écriture en base est **incrémentale** : la question de l'opérateur
        est enregistrée avant le premier appel au modèle, chaque lecture au
        moment où elle revient. Un onglet fermé au milieu laisse donc une
        conversation fidèle à ce qui a réellement eu lieu — alors qu'une
        écriture unique en fin de tour aurait tout perdu, y compris la question.
        """
        conversation_id, history = await self._conversation(request)
        turn = _Turn()
        yield StreamStarted(
            conversation_id=conversation_id,
            mode=request.mode,
            allow_actions=request.allow_actions,
            started_at=datetime.now(UTC),
        )

        await self._save(
            conversation_id,
            [ConversationMessage(role=MessageRole.OPERATOR, content=request.message)],
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(request.mode, allow_actions=request.allow_actions)},
            *(_as_chat_message(message) for message in history),
            {
                "role": "user",
                "content": build_user_message(
                    message=request.message,
                    tms_ids=request.tms_ids,
                    mission_id=request.mission_id,
                    alert_id=str(request.alert_id) if request.alert_id else None,
                ),
            },
        ]

        async for event in self._converse(request, messages, turn, conversation_id):
            yield event

        await self._save(
            conversation_id,
            [ConversationMessage(role=MessageRole.ASSISTANT, content=turn.answer)],
        )
        yield StreamDone(
            answer=self._answer(
                conversation_id,
                request.mode,
                turn.answer,
                turn,
                unavailable=turn.unavailable,
            )
        )

    def _answer(
        self,
        conversation_id: uuid.UUID,
        mode: AssistantMode,
        answer: str,
        turn: _Turn,
        *,
        unavailable: bool = False,
    ) -> AssistantAnswer:
        """Assemble la réponse, et en déduit l'état du tour.

        L'état est **dérivé** et non déclaré : une proposition enregistrée fait
        un tour en attente de validation, point. Un drapeau posé à la main
        finirait par mentir — dire « répondu » alors qu'une action attend
        laisserait l'opérateur croire que tout est fait.
        """
        if unavailable:
            status = AssistantStatus.UNAVAILABLE
        elif turn.pending:
            status = AssistantStatus.AWAITING_CONFIRMATION
        else:
            status = AssistantStatus.ANSWERED
        return AssistantAnswer(
            conversation_id=conversation_id,
            answer=answer,
            mode=mode,
            status=status,
            sources=tuple(turn.sources),
            actions=tuple(turn.actions),
            pending_actions=tuple(turn.pending),
            unavailable=unavailable,
            truncated=turn.truncated,
            rounds=turn.rounds,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            answered_at=datetime.now(UTC),
        )

    async def decide(
        self,
        action_id: uuid.UUID,
        *,
        operator_id: str,
        decision: ActionDecision,
    ) -> AssistantAnswer:
        """Tranche une action proposée, et rend la réponse complète.

        Comme `respond`, consomme le flux : un seul chemin décide, exécute et
        persiste, qu'on diffuse ou non.
        """
        answer: AssistantAnswer | None = None
        async for event in self.decide_stream(action_id, operator_id=operator_id, decision=decision):
            if isinstance(event, StreamDone):
                answer = event.answer
        if answer is None:  # pragma: no cover - le flux finit toujours par `done`
            raise AssistantFailed("Flux interrompu sans réponse.")
        return answer

    async def decide_stream(
        self,
        action_id: uuid.UUID,
        *,
        operator_id: str,
        decision: ActionDecision,
    ) -> AsyncIterator[AssistantEvent]:
        """Tranche une action proposée, et poursuit la conversation en diffusant.

        C'est la reprise après interruption : l'action validée s'exécute avec
        ses **arguments figés**, son résultat est versé dans l'historique, puis
        le modèle est rappelé pour en rendre compte — et, s'il reste une étape,
        pour proposer la suivante. Un plan en plusieurs gestes se déroule ainsi
        sans qu'aucun état ne vive en mémoire : la conversation *est* l'état.
        """
        async with self._database.connection() as connection:
            known = await self._pending.get(connection, action_id, operator_id=operator_id)
        if known is None:
            # Action d'un collègue, ou identifiant inconnu : le même message
            # dans les deux cas, pour ne pas révéler ce qui existe ailleurs.
            logger.warning("Décision sur une action hors périmètre de %s", operator_id)
            yield StreamDone(answer=self._decision_answer(None, ALREADY_DECIDED))
            return

        yield StreamStarted(
            conversation_id=known.conversation_id,
            mode=AssistantMode.CHAT,
            allow_actions=True,
            started_at=datetime.now(UTC),
        )

        if decision is ActionDecision.REJECT:
            async with self._database.transaction() as connection:
                rejected = await self._pending.reject(connection, action_id, operator_id=operator_id)
            if not rejected:
                yield StreamDone(answer=self._decision_answer(known, ALREADY_DECIDED))
                return
            await self._save(
                known.conversation_id,
                [
                    ConversationMessage(
                        role=MessageRole.OPERATOR,
                        content=f"[action refusée] {known.summary}",
                    ),
                    ConversationMessage(role=MessageRole.ASSISTANT, content=REJECTED_ANSWER),
                ],
            )
            logger.info("Action %s refusée par %s", action_id, operator_id)
            yield TextDelta(text=REJECTED_ANSWER)
            yield StreamDone(answer=self._decision_answer(known, REJECTED_ANSWER))
            return

        async with self._database.transaction() as connection:
            claimed = await self._pending.claim(connection, action_id, operator_id=operator_id)
        if claimed is None:
            # Déjà tranchée, périmée, ou validée par un autre onglet une
            # fraction de seconde plus tôt. Dans tous les cas, rien n'a été
            # exécuté deux fois : c'est la propriété que porte `claim`.
            yield StreamDone(answer=self._decision_answer(known, ALREADY_DECIDED))
            return

        role = ToolRole.ACTION
        yield ToolStarted(
            call_id=str(claimed.id),
            tool=claimed.kind.value,
            role=role,
            arguments=summarize_arguments(claimed.arguments),
        )
        started = time.monotonic()
        payload, executed = await self._actions.execute(
            claimed.kind.value,
            claimed.arguments,
            ActionContext(
                operator_id=operator_id,
                mission_id=known.mission_id,
                alert_id=known.alert_id,
                # L'identité de l'appel est celle de la validation : rejouer
                # cette validation ne refait pas sonner le téléphone.
                idempotency_override=f"action:{claimed.id}",
            ),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        succeeded = bool(executed and executed.succeeded)
        async with self._database.transaction() as connection:
            await self._pending.record_result(
                connection,
                claimed.id,
                succeeded=succeeded,
                payload=payload,
                failure_reason=None if succeeded else str(payload.get("detail") or payload.get("error")),
            )
        logger.info(
            "Action %s %s par %s",
            claimed.kind.value,
            "exécutée" if succeeded else "en échec",
            operator_id,
        )
        rendered_payload = json.dumps(payload, ensure_ascii=False, default=str)
        yield ToolCompleted(
            call_id=str(claimed.id),
            tool=claimed.kind.value,
            role=role,
            ok=succeeded,
            duration_ms=duration_ms,
            preview=preview(rendered_payload),
        )

        async for event in self._resume(known, claimed, payload, executed, operator_id):
            yield event

    async def _resume(
        self,
        pending: PendingAction,
        claimed: ClaimedAction,
        payload: dict[str, Any],
        executed: ExecutedAction | None,
        operator_id: str,
    ) -> AsyncIterator[AssistantEvent]:
        """Rend compte de l'action exécutée, et laisse la suite se dérouler.

        Le modèle est rappelé avec l'historique et le résultat réel. S'il reste
        une étape — « appelle le chauffeur, puis clos l'alerte selon sa
        réponse » — il la propose, et une nouvelle validation est demandée.
        """
        turn = _Turn()
        if executed is not None:
            turn.actions.append(executed)
        rendered_payload = json.dumps(payload, ensure_ascii=False, default=str)
        await self._save(
            pending.conversation_id,
            [
                ConversationMessage(role=MessageRole.OPERATOR, content=f"[action validée] {pending.summary}"),
                ConversationMessage(
                    role=MessageRole.TOOL,
                    content=rendered_payload,
                    tool_name=claimed.kind.value,
                ),
            ],
        )

        async with self._database.connection() as connection:
            history = await self._repository.history(
                connection, pending.conversation_id, limit=self._history_limit
            )
        request = AssistantRequest(
            operator_id=operator_id,
            message=f"J'ai validé : {pending.summary}. Résultat ci-dessus.",
            mode=AssistantMode.CHAT,
            conversation_id=pending.conversation_id,
            # Le droit d'agir reste ouvert : un plan en plusieurs gestes doit
            # pouvoir proposer l'étape suivante. Elle sera, elle aussi,
            # soumise à validation.
            allow_actions=True,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(AssistantMode.CHAT, allow_actions=True)},
            *(_as_chat_message(message) for message in history),
            {
                "role": "user",
                "content": (
                    f"L'opérateur a validé : {pending.summary}\n"
                    f"Résultat de l'exécution : {rendered_payload}\n"
                    "Dis-lui en une phrase ce qui a été fait. S'il reste une étape "
                    "à sa demande initiale, propose-la ; sinon, n'appelle aucun outil."
                ),
            },
        ]
        async for event in self._converse(request, messages, turn, pending.conversation_id):
            yield event

        await self._save(
            pending.conversation_id,
            [ConversationMessage(role=MessageRole.ASSISTANT, content=turn.answer)],
        )
        yield StreamDone(
            answer=self._answer(
                pending.conversation_id,
                AssistantMode.CHAT,
                turn.answer,
                turn,
                unavailable=turn.unavailable,
            )
        )

    def _decision_answer(self, pending: PendingAction | None, message: str) -> AssistantAnswer:
        """Réponse à une décision qui n'appelle aucune inférence.

        Un refus ou une action déjà tranchée n'ont pas à coûter un appel au
        modèle : ce que l'opérateur doit lire est factuel, et le lui faire
        rédiger par un LLM ne ferait qu'ajouter du délai et de l'incertitude.
        """
        return AssistantAnswer(
            conversation_id=pending.conversation_id if pending else uuid.uuid4(),
            answer=message,
            mode=AssistantMode.CHAT,
            status=AssistantStatus.ANSWERED,
            answered_at=datetime.now(UTC),
        )

    async def waiting_actions(self, operator_id: str, *, limit: int = 20) -> list[PendingAction]:
        """Ce qui attend la validation de cet opérateur.

        La question qu'un agent de suivi pose en ouvrant son écran, et qui ne
        tient pas dans la continuité d'une requête HTTP : d'où des lignes en
        base plutôt qu'un état suspendu en mémoire.
        """
        async with self._database.connection() as connection:
            return await self._pending.waiting(connection, operator_id, limit=limit)

    async def sweep_actions(self) -> dict[str, int]:
        """Solde les propositions périmées et les exécutions perdues."""
        async with self._database.transaction() as connection:
            return await self._pending.sweep(connection)

    async def _append(
        self,
        conversation_id: uuid.UUID,
        messages: list[ConversationMessage],
    ) -> None:
        async with self._database.transaction() as connection:
            await self._repository.append(connection, conversation_id, messages)

    async def history(
        self,
        conversation_id: uuid.UUID,
        operator_id: str,
        *,
        limit: int = 100,
    ) -> list[ConversationMessage]:
        """Historique d'une conversation, pour l'afficher à son propriétaire."""
        async with self._database.connection() as connection:
            if not await self._repository.belongs_to(connection, conversation_id, operator_id):
                return []
            return await self._repository.history(connection, conversation_id, limit=limit)

    # --- Conversation ---------------------------------------------------------

    async def _conversation(
        self,
        request: AssistantRequest,
    ) -> tuple[uuid.UUID, list[ConversationMessage]]:
        """Ouvre ou poursuit la conversation, et relit son historique.

        Une conversation dont l'identifiant ne correspond pas à cet opérateur
        est traitée comme une nouvelle : refuser serait plus bruyant, mais
        poursuivre l'historique d'un collègue serait une fuite. En ouvrir une
        neuve ne perd rien de ce que l'opérateur voulait demander.
        """
        async with self._database.transaction() as connection:
            if request.conversation_id is not None:
                if await self._repository.belongs_to(
                    connection, request.conversation_id, request.operator_id
                ):
                    history = await self._repository.history(
                        connection, request.conversation_id, limit=self._history_limit
                    )
                    return request.conversation_id, history
                logger.warning(
                    "Conversation %s hors du périmètre de %s : une nouvelle est ouverte",
                    request.conversation_id,
                    request.operator_id,
                )
            conversation_id = await self._repository.open(
                connection,
                operator_id=request.operator_id,
                title=_title(request),
                mode=request.mode,
            )
        return conversation_id, []

    async def _converse(
        self,
        request: AssistantRequest,
        messages: list[dict[str, Any]],
        turn: _Turn,
        conversation_id: uuid.UUID,
    ) -> AsyncIterator[AssistantEvent]:
        """Mène les tours de lecture jusqu'à une réponse, en diffusant.

        L'issue est déposée sur le tour (`turn.answer`, `turn.unavailable`) :
        un générateur ne peut pas rendre de valeur, et un type sentinelle
        glissé dans le flux obligerait chaque consommateur à le filtrer.
        """
        tools = await self._tools(request)
        trace = {
            "operator_id": request.operator_id,
            "mode": request.mode.value,
            "allow_actions": request.allow_actions,
        }
        turn.answer = NO_ANSWER

        for round_number in range(1, self._max_rounds + 1):
            turn.rounds = round_number
            last_round = round_number == self._max_rounds
            if last_round:
                messages.append({"role": "user", "content": FINAL_ROUND_NUDGE})
            yield RoundStarted(round=round_number, max_rounds=self._max_rounds)

            reply: LlmReply | None = None
            # Dernier tour : plus aucun outil, pour que le modèle réponde avec
            # ce qu'il a au lieu de lire indéfiniment.
            round_tools = None if last_round else tools
            try:
                async for event, chunk in self._stream_round(
                    messages, round_tools, trace, last_round=last_round
                ):
                    if chunk is not None:
                        reply = chunk
                    elif event is not None:
                        yield event
            except LlmCallFailed as failure:
                logger.warning("Assistant indisponible : %s", failure.message)
                turn.answer = UNAVAILABLE_ANSWER
                turn.unavailable = True
                return
            if reply is None:  # pragma: no cover - le flux finit par la réponse
                turn.answer = NO_ANSWER
                return

            turn.input_tokens += reply.usage.input_tokens
            turn.output_tokens += reply.usage.output_tokens

            if not reply.wants_tools:
                content = (reply.content or "").strip()
                if content:
                    if reply.finish_reason == "length":
                        turn.truncated = True
                        content += TRUNCATION_NOTICE
                        # La coupure est annoncée dans le flux aussi : un
                        # client qui n'écoute que les fragments verrait sinon
                        # une phrase s'arrêter sans explication.
                        yield TextDelta(text=TRUNCATION_NOTICE)
                    turn.answer = content
                    return
                # Ni outil ni texte : le modèle n'a rien produit. On le relance
                # une fois plutôt que de rendre une réponse vide.
                messages.append({"role": "user", "content": FINAL_ROUND_NUDGE})
                continue

            messages.append(_assistant_message(reply))
            for call in reply.tool_calls:
                async for event in self._run_tool(call, request, turn, conversation_id, messages):
                    yield event

        turn.answer = NO_ANSWER

    async def _run_tool(
        self,
        call: ToolCall,
        request: AssistantRequest,
        turn: _Turn,
        conversation_id: uuid.UUID,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AssistantEvent]:
        """Exécute un appel d'outil, en annonçant son début et sa fin.

        La durée est mesurée : c'est elle qui explique à l'opérateur pourquoi
        une réponse tarde, et au développeur quel TMS répond mal.
        """
        role = ToolRole.ACTION if call.name in self._actions.names else ToolRole.READ
        yield ToolStarted(
            call_id=call.id,
            tool=call.name,
            role=role,
            arguments=summarize_arguments(call.arguments),
        )
        proposals = len(turn.pending)
        started = time.monotonic()
        content = await self._dispatch(call.name, call.arguments, request, turn, conversation_id)
        duration_ms = int((time.monotonic() - started) * 1000)

        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": content,
            }
        )
        # Enregistrée immédiatement : une lecture qui a eu lieu doit rester
        # vérifiable même si l'opérateur ferme son onglet juste après.
        await self._save(
            conversation_id,
            [
                ConversationMessage(
                    role=MessageRole.TOOL,
                    content=content[: self._max_tool_result_chars],
                    tool_name=call.name,
                )
            ],
        )
        yield ToolCompleted(
            call_id=call.id,
            tool=call.name,
            role=role,
            ok='"ok": false' not in content and '"ok":false' not in content,
            duration_ms=duration_ms,
            preview=preview(content),
        )
        # Une action préparée pendant cet appel : annoncée tout de suite, pour
        # que le bouton de validation apparaisse sans attendre la fin du tour.
        for pending in turn.pending[proposals:]:
            yield PendingActionProposed(action=pending)

    async def _stream_round(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        trace: dict[str, Any],
        *,
        last_round: bool,
    ) -> AsyncIterator[tuple[AssistantEvent | None, LlmReply | None]]:
        """Diffuse un tour, en tenant dans la fenêtre du modèle.

        L'historique est compacté **avant** l'envoi. Si le fournisseur refuse
        tout de même pour cette raison, une seconde tentative resserrée est
        faite — mais seulement si **rien n'a encore été diffusé** : rejouer un
        tour dont des fragments sont partis ferait bégayer la réponse sous les
        yeux de l'opérateur.
        """
        emitted = False
        try:
            async for chunk in self._llm.complete_stream(
                self._budget.fit(messages, tools),
                tools=tools,
                tool_choice=None if last_round else "auto",
                trace=trace,
            ):
                if chunk.reply is not None:
                    yield None, chunk.reply
                elif chunk.reasoning:
                    emitted = True
                    yield ReasoningDelta(text=chunk.reasoning), None
                elif chunk.text:
                    emitted = True
                    yield TextDelta(text=chunk.text), None
            return
        except LlmCallFailed as failure:
            if not failure.context_overflow or emitted:
                raise
            logger.warning("Fenêtre dépassée malgré la compaction : seconde tentative resserrée")

        tighter = ContextBudget(int(self._budget.limit * TIGHTER_BUDGET_RATIO))
        async for chunk in self._llm.complete_stream(
            tighter.fit(messages, tools),
            tools=tools,
            tool_choice=None if last_round else "auto",
            trace=trace,
        ):
            if chunk.reply is not None:
                yield None, chunk.reply
            elif chunk.reasoning:
                yield ReasoningDelta(text=chunk.reasoning), None
            elif chunk.text:
                yield TextDelta(text=chunk.text), None

    async def _tools(self, request: AssistantRequest) -> list[dict[str, Any]]:
        """Outils présentés au modèle : lectures, et actions si autorisées.

        L'autorisation retire les outils d'action de la liste au lieu de les
        refuser à l'exécution. Un outil absent ne peut pas être appelé par
        zèle, et n'occupe pas la fenêtre de contexte le reste du temps.
        """
        tools = await self._mcp.tool_definitions()
        if request.allow_actions:
            tools.extend(self._actions.definitions())
        return tools

    async def _dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        request: AssistantRequest,
        turn: _Turn,
        conversation_id: uuid.UUID,
    ) -> str:
        """Route un appel : action de l'assistant, ou lecture MCP."""
        if name in self._actions.names:
            if not request.allow_actions:
                # Ne devrait pas arriver, l'outil n'étant pas présenté. Un
                # modèle peut toutefois inventer un nom d'outil : le refus
                # explicite vaut mieux qu'une action exécutée sans mandat.
                logger.warning("Action %s demandée sans autorisation", name)
                return json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "message": (
                                "Action non autorisée pour cette demande. L'opérateur "
                                "doit l'autoriser explicitement."
                            )
                        },
                    },
                    ensure_ascii=False,
                )
            return await self._act(name, arguments, request, turn, conversation_id)

        result = await self._mcp.call(name, arguments)
        turn.sources.append(ConsultedSource(tool=name, summary="" if result.ok else "lecture en échec"))
        return result.as_message_content(max_chars=self._max_tool_result_chars)

    async def _act(
        self,
        name: str,
        arguments: dict[str, Any],
        request: AssistantRequest,
        turn: _Turn,
        conversation_id: uuid.UUID,
    ) -> str:
        """Prépare ou exécute une action, selon ce qu'elle engage.

        Les gestes qui **sortent du système** — clore une alerte, faire sonner
        un téléphone — ne sont jamais exécutés par le modèle. Ils sont
        enregistrés avec leurs arguments figés et soumis à l'opérateur, qui
        valide ce qu'il lit. Le modèle ne repasse pas à la validation : il ne
        peut donc pas substituer une autre alerte à celle qui a été montrée.
        """
        kind = ActionKind(name) if name in {item.value for item in ActionKind} else None
        if kind is None or kind not in REQUIRES_CONFIRMATION:
            payload, executed = await self._actions.invoke(name, arguments, request)
            if executed is not None:
                turn.actions.append(executed)
            return json.dumps(payload, ensure_ascii=False, default=str)

        error, proposal = await self._actions.prepare(name, arguments, request)
        if proposal is None:
            # Soit la demande est irrecevable, soit le geste est sans objet
            # (alerte déjà close) : dans les deux cas, inutile de déranger
            # l'opérateur. Le modèle lit pourquoi et poursuit.
            return json.dumps(error or {"ok": False}, ensure_ascii=False, default=str)

        async with self._database.transaction() as connection:
            pending = await self._pending.propose(
                connection,
                conversation_id=conversation_id,
                operator_id=request.operator_id,
                kind=proposal.kind,
                arguments=proposal.arguments,
                summary=proposal.summary,
                reason=proposal.reason,
                tms_id=proposal.tms_id,
                mission_id=proposal.mission_id,
                alert_id=proposal.alert_id,
                ttl=self._action_ttl,
            )
        turn.pending.append(pending)
        logger.info(
            "Action %s proposée à %s : %s",
            pending.kind.value,
            request.operator_id,
            pending.summary,
        )
        return json.dumps(
            {
                "ok": True,
                "status": "awaiting_confirmation",
                "action_id": str(pending.id),
                "summary": pending.summary,
                "note": (
                    "Action préparée et soumise à l'opérateur. Elle n'est PAS "
                    "exécutée. Annonce-lui ce que tu proposes, en une phrase, et "
                    "n'appelle plus d'outil."
                ),
            },
            ensure_ascii=False,
        )

    # --- Persistance ----------------------------------------------------------

    async def _save(
        self,
        conversation_id: uuid.UUID,
        messages: list[ConversationMessage],
    ) -> None:
        """Ajoute des messages à l'historique, sans jamais faire échouer le tour (F25).

        Un historique non écrit ne doit pas priver l'opérateur de sa réponse :
        elle est déjà produite, elle part quand même. L'incident est journalisé
        — c'est la base qui est en cause, pas la conversation.
        """
        try:
            await self._append(conversation_id, messages)
        except Exception:
            logger.exception("Historique de la conversation %s non enregistré", conversation_id)


def _title(request: AssistantRequest) -> str:
    """Titre d'une conversation : sa première question, tronquée."""
    if request.mode is AssistantMode.REPORT:
        return "Rapport de situation"
    return " ".join(request.message.split())[:120]


def _as_chat_message(message: ConversationMessage) -> dict[str, Any]:
    """Traduit un message conservé en message d'inférence.

    Les lectures d'outil sont relues comme des messages `user` préfixés, et non
    comme des messages `tool` : un message `tool` sans l'appel qui l'a provoqué
    fait rejeter la conversation entière par l'API, et l'appel, lui, n'est pas
    conservé — ce qui compte pour la suite est ce que la lecture a donné.
    """
    if message.role is MessageRole.ASSISTANT:
        return {"role": "assistant", "content": message.content}
    if message.role is MessageRole.TOOL:
        return {
            "role": "user",
            "content": f"[lecture précédente — {message.tool_name}] {message.content}",
        }
    return {"role": "user", "content": message.content}


def _assistant_message(reply: LlmReply) -> dict[str, Any]:
    """Reconstruit le tour du modèle, appels d'outils compris."""
    message: dict[str, Any] = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw_arguments or json.dumps(call.arguments),
                },
            }
            for call in reply.tool_calls
        ]
    return message
