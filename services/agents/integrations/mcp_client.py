"""Pont entre les outils MCP et le format d'appel d'outils du modèle.

C'est ici que se joue l'objectif 6 de la spécification : **l'agent découvre les
outils dynamiquement et n'a aucun code spécifique au TMS**. Les outils ne sont
pas énumérés dans ce fichier — ils sont demandés au serveur MCP, puis traduits
dans le format attendu par le modèle. Ajouter un outil au serveur suffit à le
rendre disponible à l'enquêteur, sans toucher au service agent.

Le client du monitoring, lui, est volontairement étroit : il n'expose que les
quelques capacités dont le cycle a besoin. Les deux formes coexistent parce que
les besoins sont opposés — le worker veut un contrat figé et typé, l'agent veut
tout ce qui existe.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastmcp import Client

from .reducers import reduce_payload

logger = logging.getLogger(__name__)

# Outils exposés à l'enquêteur. Une **liste blanche** et non une liste noire :
# un outil ajouté au serveur MCP ne doit pas se retrouver dans la fenêtre de
# contexte de l'enquêteur par simple oubli.
#
# Ce sous-ensemble est l'un des trois leviers de maîtrise du volume de jetons
# nommés par le document d'architecture, et ce n'est pas une optimisation
# prématurée : les schémas des outils sont **renvoyés à chaque tour** avec
# l'historique accumulé. Mesuré sur une enquête réelle, passer de onze à six
# outils change l'ordre de grandeur de la consommation — et sur le GPU de
# développement, dont la fenêtre est de 4096 jetons, c'est la différence entre
# une enquête qui aboutit et une enquête refusée.
#
# Sont volontairement exclus :
#   - `get_monitoring_cycle` : rendrait toute la flotte, il est fait pour le
#     worker et non pour un raisonnement sur une mission (§20) ;
#   - `get_active_missions` : l'enquête porte sur une mission déjà désignée ;
#   - `resolve_recipients` : l'enquêteur ne notifie personne (F26), et les
#     destinataires déjà joints lui sont donnés dans son contexte initial ;
#   - `list_tms` : son `tms_id` lui est donné.
INVESTIGATOR_TOOLS: frozenset[str] = frozenset(
    {
        "get_mission_snapshot",
        "get_mission_events",
        "get_position_history",
        "get_route",
        "get_driver",
        "get_vehicle",
    }
)

# Taille maximale d'un résultat d'outil remis au modèle, **après** réduction par
# le sens (cf. `reducers`). La troncature par longueur n'est plus que le dernier
# filet : elle est aveugle, et c'est elle qui avait coupé le champ `deviation`
# d'un instantané réel — exactement la réponse attendue. Le plafond est calé sur
# un instantané réel réduit (environ 3 000 caractères, soit ~750 jetons) pour
# qu'un cas normal n'y touche jamais. La troncature reste annoncée dans la
# charge utile, afin que le modèle sache qu'il n'a pas tout vu.
MAX_TOOL_RESULT_CHARS = 3500


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Résultat d'un appel d'outil, prêt à être rendu au modèle."""

    name: str
    payload: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.payload.get("ok") is not False

    def as_message_content(self, *, max_chars: int = 0) -> str:
        """Sérialise le résultat pour le remettre au modèle.

        En JSON et non en prose : le modèle doit pouvoir lire `ok`,
        `error.code` et `error.retryable` sans les deviner dans une phrase.

        Au-delà de `max_chars`, la charge est tronquée et la troncature
        **annoncée** : un modèle qui ignore qu'il n'a pas tout vu conclurait de
        l'absence, ce que le prompt lui interdit par ailleurs.
        """
        rendered = json.dumps(self.payload, ensure_ascii=False, default=str)
        if not max_chars or len(rendered) <= max_chars:
            return rendered
        return json.dumps(
            {
                "ok": self.ok,
                "truncated": True,
                "note": (
                    f"Résultat tronqué à {max_chars} caractères pour tenir dans la "
                    "fenêtre de contexte. Ne rien conclure de ce qui manque ; "
                    "demander une ressource plus ciblée si nécessaire."
                ),
                "partial": rendered[:max_chars],
            },
            ensure_ascii=False,
        )


class AgentMcpClient:
    """Découvre les outils MCP et exécute les appels que le modèle demande."""

    def __init__(self, target: Any, *, allowed: frozenset[str] = INVESTIGATOR_TOOLS) -> None:
        self._client = Client(target)
        self._allowed = allowed

    async def __aenter__(self) -> "AgentMcpClient":
        await self._client.__aenter__()  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.__aexit__(*exc_info)  # type: ignore[no-untyped-call]

    async def tool_definitions(self) -> list[dict[str, Any]]:
        """Outils disponibles, au format d'appel d'outils du modèle.

        La description vient du serveur : c'est elle qui apprend au modèle
        quand employer l'outil et comment lire sa réponse. Un outil sans
        description serait inutilisable, raison pour laquelle le serveur en
        impose une.
        """
        definitions: list[dict[str, Any]] = []
        for tool in await self._client.list_tools():
            if tool.name not in self._allowed:
                continue
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or tool.name,
                        "parameters": tool.input_schema or {"type": "object", "properties": {}},
                    },
                }
            )
        return sorted(definitions, key=lambda item: item["function"]["name"])

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Exécute un appel d'outil et rend sa charge utile.

        Une erreur d'exécution est **rendue comme donnée**, jamais levée : le
        modèle doit pouvoir lire qu'il s'est trompé de nom d'outil ou
        d'argument, et se corriger au tour suivant. Lever perdrait l'enquête
        sur une faute que le modèle savait rattraper.
        """
        if name not in self._allowed:
            return ToolResult(
                name,
                {
                    "ok": False,
                    "error": {
                        "code": "NOT_PERMITTED",
                        "message": (
                            f"L'outil '{name}' n'est pas accessible à l'enquêteur. "
                            f"Disponibles : {', '.join(sorted(self._allowed))}."
                        ),
                        "retryable": False,
                    },
                },
            )

        try:
            result = await self._client.call_tool(name, arguments)
        except Exception as exception:
            logger.warning("Appel d'outil '%s' en échec : %s", name, exception)
            return ToolResult(
                name,
                {
                    "ok": False,
                    "error": {
                        "code": "INVALID_ARGUMENT",
                        "message": f"Appel de '{name}' impossible : {exception}",
                        "retryable": False,
                    },
                },
            )

        # Réduction **par le sens** avant toute troncature par longueur : la
        # troncature par caractères avait coupé, en conditions réelles, le
        # champ `deviation` d'un instantané — exactement celui qui portait la
        # réponse. Voir `reducers`.
        return ToolResult(name, reduce_payload(name, _unwrap(result)))


def _unwrap(result: Any) -> dict[str, Any]:
    """Extrait la charge utile d'un résultat d'outil FastMCP."""
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        inner = payload.get("result", payload)
        if isinstance(inner, dict):
            return inner
    content = getattr(result, "content", None)
    if content:
        try:
            decoded = json.loads(content[0].text)
        except (ValueError, AttributeError, IndexError):
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    return {
        "ok": False,
        "error": {
            "code": "TMS_UNAVAILABLE",
            "message": "Réponse d'outil illisible.",
            "retryable": True,
        },
    }
