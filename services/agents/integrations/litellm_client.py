"""Client d'inférence, via le proxy LiteLLM.

Le service agent ne connaît qu'un **nom logique de modèle**
(`mission-investigator`). Le routage, le repli vers un second fournisseur, les
quotas et l'envoi des traces à Langfuse vivent dans la configuration de
LiteLLM. Passer du GPU local à une API managée ne touche donc pas ce code.

Le contrat d'erreur est le point important de ce module. Trois issues, et les
confondre aurait des conséquences opposées :

  - **indisponible** (pas de clé, quota épuisé, service éteint) : l'enquête est
    *sautée*. L'alerte reste valable et notifiée, seule l'analyse manque. Sans
    LLM, la détection continue — c'est une exigence de la spécification, pas
    une dégradation à masquer ;
  - **passager** (délai dépassé, 5xx) : à réessayer ;
  - **définitif** (requête invalide, modèle inconnu) : inutile de réessayer, et
    c'est un défaut de notre côté qu'il faut voir dans les journaux.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({408, 409, 500, 502, 503, 504})

# Quota ou débit épuisé. Traité comme une **indisponibilité** et non comme un
# incident passager : ce n'est pas un défaut de notre côté, et l'enquête doit
# être *sautée* puis reprise plus tard, non marquée en échec. Mesuré en
# conditions réelles sur le palier gratuit de Groq (7 000 jetons par minute) :
# classé passager, trois 429 consécutifs épuisaient les tentatives d'une alerte
# et la privaient définitivement d'analyse.
QUOTA_STATUSES = frozenset({429})

# Plafond de jetons produits par tour. **Toujours envoyé**, et c'est une
# correction mesurée : sans lui, le modèle peut générer jusqu'au bout de la
# fenêtre. Sur une fenêtre de 4 096 jetons cela restait invisible ; passée à
# 32 768, une conclusion d'enquête a mis plus de dix minutes — le modèle
# remplissait l'espace disponible au lieu de s'arrêter. Avec un plafond, la
# même conclusion revient en quinze secondes.
#
# La valeur couvre une évaluation complète — mesurée à ~700 jetons — avec de la
# marge. Une génération coupée n'est pas perdue : la boucle rend la soumission
# non conforme au modèle, qui la corrige au tour suivant.
DEFAULT_MAX_OUTPUT_TOKENS = 1200


# Marqueurs d'un refus pour fenêtre de contexte dépassée. Le code HTTP est 400,
# qu'on ne peut pas réessayer tel quel — mais la cause est **chez nous**, et un
# historique compacté passe. D'où une qualification à part : ni panne du
# fournisseur, ni défaut irrécupérable.
# Les formulations viennent des messages réellement reçus : LiteLLM pour le
# modèle local, Groq pour le repli. Les deux disent la même chose dans des
# termes différents, et LiteLLM concatène les deux échecs dans un seul message.
CONTEXT_OVERFLOW_MARKERS = (
    "context_window_exceeded",
    "contextwindowexceeded",
    "maximum context length",
    "context length exceeded",
    "reduce the length of the",
    "reduce your message size",
    "request too large",
    "too many tokens",
)


class LlmErrorKind(StrEnum):
    UNAVAILABLE = "unavailable"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    # Requête trop longue pour la fenêtre du modèle. L'appelant peut compacter
    # son historique et réessayer : c'est la seule erreur qu'il sait corriger
    # lui-même.
    CONTEXT_OVERFLOW = "context_overflow"


class LlmCallFailed(Exception):
    """Échec d'un appel d'inférence, qualifié pour l'appelant."""

    def __init__(self, kind: LlmErrorKind, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.kind is LlmErrorKind.TRANSIENT

    @property
    def context_overflow(self) -> bool:
        """La requête était trop longue. Compacter puis réessayer a du sens."""
        return self.kind is LlmErrorKind.CONTEXT_OVERFLOW

    @property
    def skip_investigation(self) -> bool:
        """Faut-il sauter l'enquête plutôt que la marquer en échec ?

        La distinction compte pour l'exploitation : une enquête *sautée* dit
        « le LLM n'était pas là », une enquête *en échec* dit « quelque chose
        ne va pas chez nous ». Les mélanger rendrait les deux illisibles.
        """
        return self.kind is LlmErrorKind.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Appel d'outil demandé par le modèle."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass(frozen=True, slots=True)
class Usage:
    """Jetons consommés. Mesure du poste de coût principal du système."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LlmReply:
    """Réponse du modèle à un tour de conversation."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    # Raisonnement exposé par le modèle, lorsqu'il en expose un. Rien n'est
    # fabriqué : la plupart des modèles laissent ce champ vide.
    reasoning: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


def _is_context_overflow(detail: str) -> bool:
    """Reconnaît un refus pour fenêtre dépassée dans le message du fournisseur.

    Sur le message et non sur un code : les fournisseurs rendent tous un 400,
    et seul le texte distingue « trop long » de « requête malformée ». Le
    message est **aussi** celui du repli, LiteLLM concaténant les deux échecs —
    reconnaître l'un ou l'autre suffit.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in CONTEXT_OVERFLOW_MARKERS)


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """Un fragment de réponse diffusée.

    Un seul type pour les trois formes que prend un fragment — du texte, du
    raisonnement, ou la réponse complète en fin de flux — parce que l'appelant
    les reçoit dans la même boucle et que trois classes l'obligeraient à
    discriminer avant de savoir s'il y a quelque chose à faire.
    """

    text: str = ""
    reasoning: str = ""
    # Renseigné uniquement sur le dernier fragment : la réponse complète,
    # identique à celle qu'aurait rendue `complete`.
    reply: LlmReply | None = None

    @property
    def final(self) -> bool:
        return self.reply is not None


class _StreamAccumulator:
    """Reconstruit une réponse complète à partir des fragments reçus.

    Les appels d'outils sont accumulés **par index** et non par ordre
    d'arrivée : un modèle qui demande deux outils d'un coup intercale leurs
    fragments, et concaténer dans l'ordre de réception mélangerait leurs
    arguments.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, dict[str, str]] = {}
        self._finish_reason: str | None = None
        self._usage = Usage()

    def feed(self, line: str) -> list[StreamChunk]:
        """Traite une ligne du flux et rend les fragments à diffuser."""
        if not line.startswith("data:"):
            # Lignes vides, commentaires de maintien en vie : ignorés.
            return []
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            return []
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Fragment d'inférence illisible, ignoré : %r", data[:120])
            return []

        if usage := event.get("usage"):
            self._usage = Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        if model := event.get("model"):
            self._model = str(model)

        choices = event.get("choices") or []
        if not choices:
            return []
        choice = choices[0]
        if reason := choice.get("finish_reason"):
            self._finish_reason = str(reason)

        delta = choice.get("delta") or {}
        chunks: list[StreamChunk] = []
        if text := delta.get("content"):
            self._content.append(str(text))
            chunks.append(StreamChunk(text=str(text)))
        # Champ normalisé par LiteLLM pour les modèles qui exposent leur
        # raisonnement. Absent partout ailleurs : rien n'est fabriqué.
        if thought := delta.get("reasoning_content"):
            self._reasoning.append(str(thought))
            chunks.append(StreamChunk(reasoning=str(thought)))
        for fragment in delta.get("tool_calls") or []:
            self._accumulate_call(fragment)
        return chunks

    def _accumulate_call(self, fragment: dict[str, Any]) -> None:
        index = int(fragment.get("index") or 0)
        slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if identifier := fragment.get("id"):
            slot["id"] = str(identifier)
        function = fragment.get("function") or {}
        if name := function.get("name"):
            slot["name"] = str(name)
        if arguments := function.get("arguments"):
            # Concaténation : les arguments arrivent coupés, parfois au milieu
            # d'une chaîne JSON. Les analyser fragment par fragment échouerait.
            slot["arguments"] += str(arguments)

    def reply(self) -> LlmReply:
        calls = [
            {
                "id": slot["id"] or slot["name"],
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
            for _, slot in sorted(self._calls.items())
            if slot["name"]
        ]
        return LlmReply(
            content="".join(self._content) or None,
            tool_calls=tuple(LiteLlmClient._parse_tool_calls(calls)),
            model=self._model,
            usage=self._usage,
            finish_reason=self._finish_reason,
            reasoning="".join(self._reasoning) or None,
        )


class LiteLlmClient:
    """Appels de complétion conversationnelle, compatibles OpenAI."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model: str,
        temperature: float = 0.1,
        timeout_s: float = 120.0,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s),
        )
        self._owns_client = client is None
        if api_key:
            self._client.headers.update({"Authorization": f"Bearer {api_key}"})

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> LlmReply:
        """Un tour de conversation, avec outils éventuels.

        `trace` est repris dans les métadonnées envoyées à Langfuse : il permet
        de relier une trace d'inférence à l'alerte et à la mission qui l'ont
        provoquée, ce qui est la seule façon d'enquêter plus tard sur le coût
        ou le comportement d'une enquête précise.
        """
        if not self._api_key:
            # Sans clé, le proxy refuserait tout : le dire ici évite un
            # aller-retour réseau et donne la bonne qualification d'emblée.
            raise LlmCallFailed(
                LlmErrorKind.UNAVAILABLE,
                "Aucune clé LiteLLM configurée : enquête sautée.",
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if trace:
            payload["metadata"] = {"trace_metadata": trace}

        try:
            response = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.TimeoutException as error:
            raise LlmCallFailed(LlmErrorKind.TRANSIENT, f"Délai dépassé à l'inférence : {error}") from error
        except httpx.HTTPError as error:
            # Proxy éteint : traité comme une indisponibilité et non comme un
            # incident passager. Réessayer en boucle n'allumerait pas le service.
            raise LlmCallFailed(
                LlmErrorKind.UNAVAILABLE, f"Proxy d'inférence injoignable : {error}"
            ) from error

        if response.status_code >= 300:
            raise self._failure_from(response)

        return self._parse(response)

    async def complete_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> AsyncIterator["StreamChunk"]:
        """Un tour de conversation, diffusé fragment par fragment.

        Rend des fragments au fil de l'eau, puis un dernier fragment portant la
        réponse complète (`reply`) — identique à ce que `complete` aurait
        rendu. L'appelant n'a donc pas deux formes à gérer : il affiche les
        fragments s'il le souhaite, et travaille sur la réponse finale.

        Deux pièges du protocole, que ce code traite explicitement :

          - **les appels d'outils arrivent fragmentés.** Le nom vient dans un
            fragment, les arguments dans plusieurs autres, parfois coupés au
            milieu d'une chaîne JSON. Ils sont accumulés par `index`, et non par
            ordre d'arrivée : un modèle qui demande deux outils d'un coup
            intercale leurs fragments ;
          - **la consommation de jetons n'arrive qu'à la fin**, et seulement si
            on la demande (`stream_options`). Sans cela, le poste de coût
            principal du système deviendrait invisible dès qu'on diffuse.
        """
        if not self._api_key:
            raise LlmCallFailed(
                LlmErrorKind.UNAVAILABLE,
                "Aucune clé LiteLLM configurée : assistant indisponible.",
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_output_tokens,
            "stream": True,
            # Sans cela, un tour diffusé ne rapporte aucun jeton et le coût
            # d'une conversation devient impossible à mesurer.
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if trace:
            payload["metadata"] = {"trace_metadata": trace}

        accumulator = _StreamAccumulator(self._model)
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=payload) as response:
                if response.status_code >= 300:
                    # Le corps n'est pas encore lu : sans cette lecture, le
                    # message d'erreur du fournisseur serait perdu et l'on
                    # rapporterait « refusé » sans dire pourquoi.
                    await response.aread()
                    raise self._failure_from(response)

                async for line in response.aiter_lines():
                    for chunk in accumulator.feed(line):
                        yield chunk
        except httpx.TimeoutException as error:
            raise LlmCallFailed(LlmErrorKind.TRANSIENT, f"Délai dépassé à l'inférence : {error}") from error
        except httpx.HTTPError as error:
            raise LlmCallFailed(
                LlmErrorKind.UNAVAILABLE, f"Proxy d'inférence injoignable : {error}"
            ) from error

        yield StreamChunk(reply=accumulator.reply())

    def _failure_from(self, response: httpx.Response) -> LlmCallFailed:
        detail = self._error_detail(response)
        status = response.status_code

        if _is_context_overflow(detail):
            # Vérifié avant le code HTTP : un dépassement de fenêtre arrive en
            # 400, que l'on classerait sinon « défaut définitif » — et l'enquête
            # serait perdue alors qu'un historique compacté passerait.
            raise_kind = LlmErrorKind.CONTEXT_OVERFLOW
        elif status in (401, 403) or status in QUOTA_STATUSES:
            raise_kind = LlmErrorKind.UNAVAILABLE
        elif status in RETRYABLE_STATUSES:
            raise_kind = LlmErrorKind.TRANSIENT
        else:
            raise_kind = LlmErrorKind.PERMANENT

        return LlmCallFailed(
            raise_kind,
            f"Inférence refusée ({status}) : {detail}",
            status_code=status,
        )

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)
            if error:
                return str(error)
        return str(body)[:300]

    def _parse(self, response: httpx.Response) -> LlmReply:
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError) as error:
            raise LlmCallFailed(LlmErrorKind.PERMANENT, f"Réponse d'inférence illisible : {error}") from error

        usage = body.get("usage") or {}
        return LlmReply(
            content=message.get("content"),
            # Lu ici comme dans le flux : un modèle à raisonnement sépare sa
            # réflexion de sa réponse, et ne pas la lire ferait croire à une
            # réponse vide quand le plafond de jetons a été consommé à penser.
            reasoning=message.get("reasoning_content"),
            tool_calls=tuple(self._parse_tool_calls(message.get("tool_calls") or [])),
            model=str(body.get("model") or self._model),
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            finish_reason=choice.get("finish_reason"),
        )

    @staticmethod
    def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[ToolCall]:
        """Lit les appels d'outils, en tolérant des arguments mal formés.

        Un modèle peut rendre un JSON invalide. L'appel est conservé avec des
        arguments vides et sa charge brute : l'exécuteur rendra une erreur
        d'outil que le modèle pourra corriger au tour suivant. Lever ici
        perdrait l'enquête pour une virgule.
        """
        calls: list[ToolCall] = []
        for raw in raw_calls:
            function = raw.get("function") or {}
            name = str(function.get("name") or "")
            if not name:
                continue
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
            except json.JSONDecodeError:
                logger.warning("Arguments d'outil illisibles pour %s : %r", name, raw_arguments)
                arguments = {}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or name),
                    name=name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    raw_arguments=str(raw_arguments),
                )
            )
        return calls
