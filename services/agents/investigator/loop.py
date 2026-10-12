"""Boucle d'enquête : raisonnement, appels d'outils, conclusion structurée.

    contexte déterministe
          -> modèle
              -> outils MCP (plusieurs tours, bornés)
          -> submit_assessment
              -> InvestigationResult

**Une boucle bornée et non un graphe d'états.** La spécification mentionne
LangGraph ; nous ne l'employons pas ici, et c'est un choix assumé. Une enquête
est un aller-retour unique sans reprise : elle n'a ni point de sauvegarde à
conserver, ni branche à reprendre après interruption — une enquête perdue est
simplement relancée depuis son socle de faits. L'intérêt de LangGraph, le
*checkpointer*, vaudra pour l'**assistant** conversationnel, dont l'historique
doit survivre à un redémarrage (F25). Introduire un graphe ici ajouterait une
indirection sans rien résoudre, et rendrait la boucle moins testable.

Trois garde-fous portent la correction de ce module :

  - **le nombre de tours est plafonné.** Un modèle qui ne conclut pas
    consommerait indéfiniment des jetons et du quota TMS ;
  - **la conclusion est forcée au dernier tour.** Mieux vaut une analyse
    prudente qu'aucune analyse ;
  - **un échec d'enquête ne touche jamais l'alerte.** Elle reste valable et
    notifiée ; seule l'explication manque.
"""

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from shared.mapped_dto import InvestigationRequest, InvestigationResult

from ..integrations.budget import (
    DEFAULT_MAX_INPUT_TOKENS,
    TIGHTER_BUDGET_RATIO,
    ContextBudget,
)
from ..integrations.litellm_client import LiteLlmClient, LlmCallFailed, LlmReply, Usage
from ..integrations.mcp_client import MAX_TOOL_RESULT_CHARS, AgentMcpClient
from .prompts import FINAL_ROUND_NUDGE, build_user_prompt, system_prompt
from .submission import SUBMIT_TOOL_NAME, InvestigationSubmission, submit_tool_definition

logger = logging.getLogger(__name__)


class LocalTool(Protocol):
    """Un outil servi par le service agent lui-même, et non par MCP.

    Il en existe un : l'appel au chauffeur, qui n'est pas une opération TMS
    (voir `services.agents.operations.tool`). Le contrat est un protocole pour
    que la boucle n'ait rien à connaître de ce qu'un outil local fait — elle
    sait seulement qu'il se nomme, se décrit et s'invoque dans le contexte de
    l'enquête courante.
    """

    name: str

    @staticmethod
    def definition() -> dict[str, Any]: ...

    async def invoke(self, request: "InvestigationRequest", arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class InvestigationOutcome:
    """Ce que la boucle a produit, succès comme échec.

    Les mesures accompagnent le résultat : le volume de jetons par enquête est
    le poste de coût principal du système, et il doit être mesuré dès le POC et
    non estimé.
    """

    result: InvestigationResult | None = None
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    rounds: int = 0
    duration_ms: int = 0
    tool_calls: int = 0
    skipped: bool = False
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None


class Investigator:
    """Conduit une enquête de bout en bout."""

    def __init__(
        self,
        llm: LiteLlmClient,
        mcp: AgentMcpClient,
        *,
        max_tool_rounds: int = 8,
        max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
        local_tools: Sequence[LocalTool] = (),
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    ) -> None:
        self._llm = llm
        self._mcp = mcp
        self._max_rounds = max_tool_rounds
        self._max_tool_result_chars = max_tool_result_chars
        self._budget = ContextBudget(max_input_tokens)
        # Indexés par nom : la boucle route chaque appel vers l'outil local
        # correspondant, et vers MCP sinon.
        self._local_tools = {tool.name: tool for tool in local_tools}

    async def investigate(self, request: InvestigationRequest) -> InvestigationOutcome:
        started = time.monotonic()
        outcome = InvestigationOutcome()

        tools = await self._mcp.tool_definitions()
        tools.extend(tool.definition() for tool in self._local_tools.values())
        submit_only = [submit_tool_definition()]
        tools.extend(submit_only)

        # Le prompt ne décrit que les outils réellement offerts : décrire un
        # outil absent invite le modèle à l'appeler, ce qui coûte un tour — et
        # sur une fenêtre étroite, un tour perdu peut être l'enquête entière.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(self._local_tools)},
            {"role": "user", "content": self._user_prompt(request)},
        ]
        # Relie la trace d'inférence à l'alerte : sans cela, un coût ou un
        # comportement anormal observé dans Langfuse serait impossible à
        # rattacher à une mission.
        trace = {
            "investigation_id": str(request.investigation_id),
            "alert_id": str(request.alert_id),
            "tms_id": request.tms_id,
            "mission_id": request.mission_id,
            "rule_id": request.alert.rule_id,
        }

        for round_number in range(1, self._max_rounds + 1):
            outcome.rounds = round_number
            last_round = round_number == self._max_rounds

            if last_round:
                # Dernier tour : on annonce la contrainte et on impose la
                # conclusion, plutôt que de rendre une enquête vide.
                messages.append({"role": "user", "content": FINAL_ROUND_NUDGE})

            try:
                reply = await self._complete(
                    messages,
                    # Dernier tour : seul l'outil de conclusion est envoyé.
                    # `tool_choice` l'impose de toute façon, et les schémas des
                    # outils de lecture pèsent plus de mille jetons — autant
                    # les retirer précisément là où la fenêtre est le plus
                    # sollicitée, après plusieurs tours d'historique.
                    tools=submit_only if last_round else tools,
                    tool_choice=(
                        {"type": "function", "function": {"name": SUBMIT_TOOL_NAME}} if last_round else "auto"
                    ),
                    trace=trace,
                )
            except LlmCallFailed as failure:
                return self._from_llm_failure(outcome, failure, started)

            outcome.model = reply.model or outcome.model
            outcome.usage = Usage(
                input_tokens=outcome.usage.input_tokens + reply.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens + reply.usage.output_tokens,
            )

            submission_call = next((call for call in reply.tool_calls if call.name == SUBMIT_TOOL_NAME), None)
            if submission_call is not None:
                result, error = self._build_result(request, submission_call.arguments)
                if result is not None:
                    outcome.result = result
                    outcome.duration_ms = int((time.monotonic() - started) * 1000)
                    return outcome
                # Soumission non conforme : on la rend au modèle pour qu'il la
                # corrige, au lieu de perdre l'enquête sur un champ mal rempli.
                messages.append(_assistant_message(reply))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": submission_call.id,
                        "name": SUBMIT_TOOL_NAME,
                        "content": json.dumps({"ok": False, "error": {"message": error}}, ensure_ascii=False),
                    }
                )
                continue

            if not reply.wants_tools:
                # Le modèle a répondu en prose sans conclure. On le ramène à
                # l'outil de conclusion : c'est la seule sortie acceptée.
                messages.append(_assistant_message(reply))
                messages.append({"role": "user", "content": FINAL_ROUND_NUDGE})
                continue

            messages.append(_assistant_message(reply))
            for call in reply.tool_calls:
                outcome.tool_calls += 1
                local = self._local_tools.get(call.name)
                if local is not None:
                    payload = await local.invoke(request, call.arguments)
                    content = json.dumps(payload, ensure_ascii=False, default=str)
                else:
                    tool_result = await self._mcp.call(call.name, call.arguments)
                    content = tool_result.as_message_content(max_chars=self._max_tool_result_chars)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": content,
                    }
                )

        outcome.error = f"Aucune conclusion après {self._max_rounds} tours."
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning("Enquête %s : %s", request.investigation_id, outcome.error)
        return outcome

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: Any,
        trace: dict[str, Any],
    ) -> LlmReply:
        """Envoie un tour, en tenant dans la fenêtre du modèle.

        L'historique est compacté **avant** l'envoi : une enquête de trois
        tours dépassait la fenêtre du modèle de développement, et le
        fournisseur refusait la requête entière — l'enquête était perdue.

        Si le fournisseur refuse malgré tout pour cette raison, notre
        estimation était optimiste : on resserre et on réessaie **une** fois.
        Au-delà, c'est un problème de dimensionnement, et le dire vaut mieux
        que boucler.
        """
        try:
            return await self._llm.complete(
                self._budget.fit(messages, tools),
                tools=tools,
                tool_choice=tool_choice,
                trace=trace,
            )
        except LlmCallFailed as failure:
            if not failure.context_overflow:
                raise
            logger.warning("Fenêtre dépassée malgré la compaction : seconde tentative resserrée")
            tighter = ContextBudget(int(self._budget.limit * TIGHTER_BUDGET_RATIO))
            return await self._llm.complete(
                tighter.fit(messages, tools),
                tools=tools,
                tool_choice=tool_choice,
                trace=trace,
            )

    @staticmethod
    def _user_prompt(request: InvestigationRequest) -> str:
        return build_user_prompt(
            tms_id=request.tms_id,
            mission_id=request.mission_id,
            alert_title=request.alert.title,
            alert_message=request.alert.message,
            rule_id=request.alert.rule_id,
            rule_version=request.alert.rule_version,
            severity=request.alert.severity.value,
            observed_facts=json.dumps(request.observed_facts, ensure_ascii=False, indent=2, default=str),
            notifications_sent=request.notifications_sent,
        )

    @staticmethod
    def _build_result(
        request: InvestigationRequest,
        arguments: dict[str, Any],
    ) -> tuple[InvestigationResult | None, str | None]:
        """Valide la soumission et y ajoute ce que le service seul connaît.

        Les identifiants ne viennent jamais du modèle : les lui confier lui
        permettrait d'attacher son analyse à une autre alerte que celle
        examinée, et l'opérateur lirait une explication qui ne correspond pas.
        """
        try:
            submission = InvestigationSubmission.model_validate(arguments)
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])} : {item['msg']}"
                for item in error.errors()[:5]
            )
            return None, f"Évaluation non conforme — {details}"

        return (
            submission.to_result(
                investigation_id=request.investigation_id,
                alert_id=request.alert_id,
            ),
            None,
        )

    @staticmethod
    def _from_llm_failure(
        outcome: InvestigationOutcome,
        failure: LlmCallFailed,
        started: float,
    ) -> InvestigationOutcome:
        """Qualifie un échec d'inférence.

        « Sauté » et « en échec » ne disent pas la même chose à l'exploitation :
        le premier signale que le LLM n'était pas là, le second qu'un défaut
        nous appartient. Les confondre rendrait les deux illisibles.
        """
        outcome.skipped = failure.skip_investigation
        outcome.error = failure.message
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        if outcome.skipped:
            logger.warning("Enquête sautée : %s", failure.message)
        else:
            logger.error("Enquête en échec : %s", failure.message)
        return outcome


def _assistant_message(reply: LlmReply) -> dict[str, Any]:
    """Reconstruit le tour du modèle, pour que la conversation reste cohérente.

    Les appels d'outils doivent y figurer à l'identique : un message `tool`
    sans l'appel qui l'a provoqué fait rejeter la conversation entière par
    l'API.
    """
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
