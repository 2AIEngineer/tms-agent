"""Budget de contexte : tenir dans la fenêtre du modèle, sans perdre l'essentiel.

Une boucle d'agent accumule : à chaque tour, l'historique renvoyé grossit du
résultat de l'outil précédent. Tôt ou tard, la fenêtre est dépassée — et le
fournisseur refuse la requête entière.

> Mesuré en conditions réelles sur le modèle de développement (fenêtre de 4 096
> jetons) : *« This model's maximum context length is 4096 tokens. However […]
> your prompt contains at least 4097 input tokens »*, au troisième tour d'une
> enquête. Le repli cloud a refusé à son tour, la requête dépassant son quota
> par minute. L'enquête était perdue.

Ce module **compacte avant d'envoyer**, plutôt que d'espérer que ça passe.
Trois principes, dans cet ordre :

  - **ce qui ne peut pas être reconstitué est gardé.** Le prompt système et le
    premier message — les faits déterministes de l'enquête, la question de
    l'opérateur — ne sont jamais touchés : sans eux, le modèle ne sait plus ce
    qu'on lui demande ;
  - **les résultats d'outils les plus anciens sont les premiers sacrifiés.**
    Un instantané lu il y a trois tours a déjà produit son effet sur le
    raisonnement ; le dernier, lui, est ce sur quoi le modèle travaille ;
  - **ce qui est retiré est annoncé.** Un résultat remplacé par un silence
    inviterait le modèle à conclure de l'absence ; remplacé par « retiré pour
    tenir dans la fenêtre », il sait qu'il peut relire.

L'appariement est préservé à tout instant : un message d'outil est **vidé**, non
supprimé, car un appel d'outil sans son résultat fait rejeter la conversation
entière par l'API. Lorsque vider ne suffit plus, ce sont des tours **complets**
— le message du modèle et tous ses résultats — qui partent ensemble.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Caractères par jeton. **Mesuré** contre le modèle servi, sur une conversation
# d'enquête réelle : 12 000 caractères pour 3 166 jetons facturés, soit 3,79
# caractères par jeton (JSON et français mêlés).
#
# La valeur retenue est volontairement plus basse, donc l'estimation plus haute,
# mais pas trop : sous-estimer coûte une requête refusée, surestimer coûte du
# contexte que le modèle aurait su employer. À 3,5, il reste environ 8 % de
# marge, à quoi s'ajoute `SAFETY_MARGIN_TOKENS`.
CHARS_PER_TOKEN = 3.5

# Marge réservée à la réponse du modèle et à l'imprécision de l'estimation.
# Un dépassement se paie d'un aller-retour perdu ; une marge se paie d'un peu
# de contexte en moins.
SAFETY_MARGIN_TOKENS = 256

# Plafond de contexte par défaut : la fenêtre du modèle servi moins la place de
# sa réponse. Aligné sur la configuration livrée (`VLLM_MAX_MODEL_LEN=32768`),
# et réglable par `AGENT_MAX_INPUT_TOKENS` — **à tenir aligné sur le modèle
# réellement servi**, sous peine de laisser le fournisseur refuser ce que nous
# aurions pu compacter.
#
# Pour mémoire, le socle d'une enquête — prompt, schémas d'outils, faits — pèse
# environ 3 750 jetons : c'est lui qui rendait une fenêtre de 4 096 impraticable.
DEFAULT_MAX_INPUT_TOKENS = 28000

# Resserrement appliqué si le fournisseur refuse malgré la compaction : notre
# estimation de jetons était optimiste, et un seul essai de plus suffit.
TIGHTER_BUDGET_RATIO = 0.6

# Ce qui remplace un résultat d'outil retiré. Court, et explicite : le modèle
# doit savoir qu'il a existé, et qu'il peut le redemander.
OMITTED_RESULT = (
    '{"ok": true, "omitted": true, "note": "Résultat retiré de l\'historique pour '
    "tenir dans la fenêtre de contexte. Rappeler l'outil si ce détail est nécessaire.\"}"
)


def estimate_tokens(value: Any) -> int:
    """Estime le nombre de jetons d'une charge utile.

    Une estimation et non une mesure : compter exactement demanderait le
    tokeniseur du modèle servi, qui change avec le modèle et que le proxy
    n'expose pas. Ce qui compte ici est de ne jamais **sous**-estimer.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return int(len(text) / CHARS_PER_TOKEN) + 1


class ContextBudget:
    """Compacte une conversation pour qu'elle tienne dans la fenêtre du modèle."""

    def __init__(self, max_input_tokens: int, *, margin: int = SAFETY_MARGIN_TOKENS) -> None:
        self._limit = max(1, max_input_tokens - margin)

    @property
    def limit(self) -> int:
        return self._limit

    def fits(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> bool:
        return self._size(messages, tools) <= self._limit

    def fit(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Rend une conversation qui tient, ou la plus petite atteignable.

        Ne modifie pas la liste reçue : l'appelant garde son historique complet,
        qui continue d'être enregistré en base. Seule la copie envoyée au modèle
        est amputée.
        """
        working = [dict(message) for message in messages]
        if self._size(working, tools) <= self._limit:
            return working

        emptied = self._empty_oldest_results(working, tools)
        if self._size(emptied, tools) <= self._limit:
            logger.info("Contexte compacté : résultats d'outils anciens retirés")
            return emptied

        dropped = self._drop_oldest_rounds(emptied, tools)
        size = self._size(dropped, tools)
        if size > self._limit:
            # Même réduit à son socle, le contexte dépasse. L'appelant doit le
            # savoir : c'est un problème de dimensionnement, pas d'historique.
            # Le socle lui-même dépasse le budget : prompt système, schémas
            # d'outils et faits de l'enquête. Aucun historique à sacrifier ne
            # changerait cela, et le dire est utile — c'est un problème de
            # dimensionnement, qui se règle par une fenêtre plus large ou moins
            # d'outils, pas par de la compaction.
            logger.warning(
                "Socle de contexte au-dessus du budget : %d jetons estimés pour %d. "
                "Élargir la fenêtre du modèle (AGENT_MAX_INPUT_TOKENS) ou réduire "
                "la liste d'outils.",
                size,
                self._limit,
            )
        else:
            logger.info("Contexte compacté : %d tour(s) ancien(s) écarté(s)", len(working) - len(dropped))
        return dropped

    # --- Étapes de compaction -------------------------------------------------

    def _empty_oldest_results(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Vide les résultats d'outils, du plus ancien au plus récent.

        Vider et non supprimer : un appel d'outil privé de son résultat fait
        rejeter la conversation entière par l'API. Le dernier résultat est
        préservé — c'est celui sur lequel le modèle travaille.
        """
        indexes = [
            index
            for index, message in enumerate(messages)
            if message.get("role") == "tool" and message.get("content") != OMITTED_RESULT
        ]
        for index in indexes[:-1] if len(indexes) > 1 else []:
            messages[index] = {**messages[index], "content": OMITTED_RESULT}
            if self._size(messages, tools) <= self._limit:
                break
        return messages

    def _drop_oldest_rounds(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Écarte des tours complets, en gardant le socle et le tour courant.

        Un tour est un message du modèle et les résultats qu'il a provoqués :
        les séparer casserait l'appariement. Le prompt système et le premier
        message de l'opérateur ne sont jamais écartés.
        """
        kept = list(messages)
        while self._size(kept, tools) > self._limit:
            start = self._first_droppable(kept)
            if start is None:
                return kept
            end = start + 1
            while end < len(kept) and kept[end].get("role") == "tool":
                end += 1
            if end >= len(kept):
                # Il ne reste que le tour courant : l'écarter n'aurait pas de
                # sens, le modèle n'aurait plus rien à quoi répondre.
                return kept
            del kept[start:end]
        return kept

    @staticmethod
    def _first_droppable(messages: list[dict[str, Any]]) -> int | None:
        """Premier tour du modèle qui n'est ni le socle ni le dernier échange."""
        seen_user = False
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "system":
                continue
            if role == "user" and not seen_user:
                # Le premier message porte les faits de l'enquête ou la
                # question de l'opérateur : il est irremplaçable.
                seen_user = True
                continue
            if role == "assistant":
                return index
        return None

    def _size(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
        total = estimate_tokens(messages)
        if tools:
            # Les schémas d'outils sont renvoyés à chaque tour : les ignorer
            # rendrait l'estimation fausse de plus de mille jetons.
            total += estimate_tokens(tools)
        return total
