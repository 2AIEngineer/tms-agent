"""Faux modèle, pour éprouver la boucle d'enquête de façon déterministe.

Un vrai LLM ne reproduit pas sur commande les comportements qu'il faut tester :
une soumission mal formée, une prose sans conclusion, un refus de conclure, un
quota épuisé. Ce double les produit tous, et rend la boucle testable sans
dépendre d'un fournisseur.
"""

import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from services.agents.integrations.litellm_client import (
    LlmCallFailed,
    LlmErrorKind,
    LlmReply,
    StreamChunk,
    ToolCall,
    Usage,
)
from services.agents.investigator.submission import SUBMIT_TOOL_NAME


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )


def valid_submission(**overrides: Any) -> dict[str, Any]:
    """Évaluation conforme, telle qu'un bon modèle la rendrait."""
    base: dict[str, Any] = {
        "summary": "Le véhicule s'est écarté de son itinéraire depuis 25 minutes.",
        "situation": "Écart de 7,2 km constaté, véhicule en mouvement, mission en cours.",
        "probable_cause": "Détour probable pour éviter un ralentissement.",
        "risk": "medium",
        "client_impact": "Retard de livraison possible d'une trentaine de minutes.",
        "recommended_actions": [
            {"action": "Appeler le chauffeur pour confirmer le détour", "urgency": "medium"}
        ],
        "evidence": [
            {
                "kind": "snapshot",
                "summary": "Écart de 7,2 km depuis 25 minutes",
                "source": "observed_facts.deviation",
            }
        ],
        "false_positive_suspicion": False,
        "confidence": 0.7,
    }
    return {**base, **overrides}


def reply(
    *,
    content: str | None = None,
    calls: Sequence[ToolCall] = (),
    input_tokens: int = 100,
    output_tokens: int = 40,
) -> LlmReply:
    return LlmReply(
        content=content,
        tool_calls=tuple(calls),
        model="faux-modele",
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def submit(**overrides: Any) -> LlmReply:
    return reply(calls=[tool_call(SUBMIT_TOOL_NAME, valid_submission(**overrides))])


@dataclass
class FakeLlm:
    """Modèle scripté : rend les réponses prévues, une par tour.

    `scenario` peut contenir des `LlmReply` ou des exceptions `LlmCallFailed`,
    ce qui permet de tester aussi bien un raisonnement qu'une panne.
    """

    scenario: list[LlmReply | LlmCallFailed | Callable[..., LlmReply]] = field(default_factory=list)
    model: str = "faux-modele"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> LlmReply:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": [t["function"]["name"] for t in (tools or [])],
                "tool_choice": tool_choice,
                "trace": trace,
            }
        )
        if not self.scenario:
            # Un scénario épuisé signale un test mal écrit, pas un
            # comportement du modèle : échouer bruyamment est préférable.
            raise AssertionError(
                f"Scénario épuisé après {len(self.calls)} appel(s) : "
                "la boucle a demandé plus de tours que prévu."
            )
        step = self.scenario.pop(0)
        if isinstance(step, LlmCallFailed):
            raise step
        if callable(step):
            return step(messages=messages, tools=tools, tool_choice=tool_choice)
        return step

    async def complete_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Rejoue la réponse prévue sous forme de fragments.

        Le texte est découpé en plusieurs morceaux : c'est ce qui éprouve le
        chemin de diffusion, un fragment unique ne dirait rien de la
        recomposition. Le dernier fragment porte la réponse complète, comme le
        vrai client.
        """
        reply = await self.complete(messages, tools=tools, tool_choice=tool_choice, trace=trace)
        if reply.reasoning:
            yield StreamChunk(reasoning=reply.reasoning)
        for fragment in _split(reply.content or ""):
            yield StreamChunk(text=fragment)
        yield StreamChunk(reply=reply)

    @property
    def rounds(self) -> int:
        return len(self.calls)

    def last_tool_choice(self) -> Any:
        return self.calls[-1]["tool_choice"] if self.calls else None


# Taille des fragments de texte rejoués. Trois mots : assez petit pour que la
# recomposition soit réellement exercée, assez grand pour que les tests restent
# lisibles.
FRAGMENT_WORDS = 3


def _split(text: str) -> list[str]:
    if not text:
        return []
    words = text.split(" ")
    fragments = []
    for index in range(0, len(words), FRAGMENT_WORDS):
        piece = " ".join(words[index : index + FRAGMENT_WORDS])
        fragments.append(piece if index == 0 else " " + piece)
    return fragments


def unavailable(message: str = "quota épuisé") -> LlmCallFailed:
    return LlmCallFailed(LlmErrorKind.UNAVAILABLE, message)


def transient(message: str = "503 du fournisseur") -> LlmCallFailed:
    return LlmCallFailed(LlmErrorKind.TRANSIENT, message)
