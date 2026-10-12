"""Tests du budget de contexte.

Défaut rencontré en exécution réelle, sur le modèle de développement dont la
fenêtre fait 4 096 jetons :

> *This model's maximum context length is 4096 tokens. However […] your prompt
> contains at least 4097 input tokens* — au troisième tour d'une enquête. Le
> repli cloud a refusé à son tour, la requête dépassant son quota par minute.
> L'enquête était perdue.

Ce qui est éprouvé ici tient en quatre propriétés, et chacune correspond à une
façon dont une compaction naïve casserait tout :

  - **l'appariement est préservé.** Un message d'outil est vidé, jamais
    supprimé : un appel d'outil sans son résultat fait rejeter la conversation
    entière par l'API ;
  - **le socle est intouchable.** Prompt système et premier message — les faits
    déterministes, la question de l'opérateur — ne sont jamais sacrifiés ;
  - **le plus récent est gardé.** Un instantané lu il y a trois tours a déjà
    produit son effet ; le dernier est ce sur quoi le modèle travaille ;
  - **ce qui est retiré est annoncé.** Un silence inviterait le modèle à
    conclure de l'absence.
"""

from services.agents.integrations.budget import (
    OMITTED_RESULT,
    ContextBudget,
    estimate_tokens,
)

SCHEMA = [{"type": "function", "function": {"name": "get_mission_snapshot", "parameters": {}}}]


def conversation(rounds: int, *, result_chars: int = 2000) -> list[dict[str, object]]:
    """Conversation d'enquête : socle, puis N tours de lecture."""
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "Tu es l'enquêteur." * 20},
        {"role": "user", "content": "Faits observés : " + "x" * 400},
    ]
    for index in range(rounds):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": "get_mission_snapshot", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "name": "get_mission_snapshot",
                "content": f"resultat-{index}-" + "y" * result_chars,
            }
        )
    return messages


def pairing_is_sound(messages: list[dict[str, object]]) -> bool:
    """Chaque appel d'outil a-t-il encore son résultat, et réciproquement ?"""
    expected: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            expected = [call["id"] for call in message.get("tool_calls") or []]  # type: ignore[index]
            continue
        if message.get("role") == "tool" and message.get("tool_call_id") not in expected:
            return False
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:  # type: ignore[union-attr]
            answers = [
                item
                for item in messages[index + 1 :]
                if item.get("role") == "tool" and item.get("tool_call_id") == call["id"]
            ]
            if not answers:
                return False
    return True


class TestEstimation:
    def test_marge_du_bon_cote(self):
        """Mesuré : 3,79 caractères par jeton sur nos charges utiles réelles.

        L'estimateur retient 3,5, donc estime **au-dessus** du réel : une
        sous-estimation coûte une requête refusée, une surestimation coûte du
        contexte que le modèle aurait su employer. La marge doit rester dans
        cet ordre de grandeur — ni absente, ni excessive.
        """
        texte = "a" * 3790

        estime = estimate_tokens(texte)

        assert estime > 1000  # au-dessus du réel mesuré (1 000 jetons)
        assert estime < 1200  # mais sans gaspiller un quart de la fenêtre

    def test_structures_comptees_comme_leur_json(self):
        charge = {"role": "tool", "content": "x" * 300}

        assert estimate_tokens(charge) > estimate_tokens("x" * 300)


class TestCompaction:
    def test_conversation_courte_inchangee(self):
        budget = ContextBudget(100_000)
        messages = conversation(2)

        assert budget.fit(messages, SCHEMA) == messages

    def test_resultats_anciens_vides_le_dernier_garde(self):
        """Le dernier résultat est celui sur lequel le modèle travaille."""
        budget = ContextBudget(1500)
        messages = conversation(4)

        compacted = budget.fit(messages, SCHEMA)

        resultats = [item for item in compacted if item.get("role") == "tool"]
        assert resultats[-1]["content"].startswith("resultat-3-")
        assert any(item["content"] == OMITTED_RESULT for item in resultats[:-1])

    def test_appariement_preserve(self):
        """Un appel d'outil sans résultat fait rejeter la conversation entière."""
        budget = ContextBudget(1200)
        messages = conversation(6)

        compacted = budget.fit(messages, SCHEMA)

        assert pairing_is_sound(compacted)

    def test_retrait_annonce(self):
        """Un silence inviterait le modèle à conclure de l'absence."""
        budget = ContextBudget(1500)

        compacted = budget.fit(conversation(4), SCHEMA)

        vides = [item for item in compacted if item.get("content") == OMITTED_RESULT]
        assert vides
        assert "Rappeler l'outil" in OMITTED_RESULT

    def test_socle_jamais_sacrifie(self):
        """Sans les faits ni le prompt, le modèle ne sait plus ce qu'on demande."""
        budget = ContextBudget(700)
        messages = conversation(8, result_chars=4000)

        compacted = budget.fit(messages, SCHEMA)

        assert compacted[0]["role"] == "system"
        assert compacted[1]["content"].startswith("Faits observés :")

    def test_tours_anciens_ecartes_quand_vider_ne_suffit_pas(self):
        budget = ContextBudget(900)
        messages = conversation(8, result_chars=3000)

        compacted = budget.fit(messages, SCHEMA)

        assert len(compacted) < len(messages)
        assert pairing_is_sound(compacted)
        # Le tour courant survit : sinon le modèle n'aurait plus rien à quoi
        # répondre.
        assert compacted[-1]["role"] == "tool"
        assert compacted[-1]["content"].startswith("resultat-7-")

    def test_les_schemas_doutils_sont_comptes(self):
        """Ils sont renvoyés à chaque tour : les ignorer fausse l'estimation.

        Même conversation, même plafond : elle tient sans les schémas et ne
        tient plus avec. Mesuré sur une enquête réelle, les six schémas d'outils
        pesaient plus de mille jetons.
        """
        budget = ContextBudget(700)
        messages = conversation(1, result_chars=200)
        gros_schema = [
            {"type": "function", "function": {"name": f"outil_{index}", "description": "d" * 400}}
            for index in range(6)
        ]

        assert budget.fits(messages, None) is True
        assert budget.fits(messages, gros_schema) is False

    def test_contexte_irreductible_rendu_tel_quel(self):
        """Un socle plus grand que la fenêtre est un problème de dimensionnement.

        Le dire — et laisser le fournisseur refuser — vaut mieux que boucler ou
        que tronquer les faits de l'enquête.
        """
        budget = ContextBudget(300)
        messages = [
            {"role": "system", "content": "s" * 5000},
            {"role": "user", "content": "u" * 5000},
        ]

        compacted = budget.fit(messages, None)

        assert compacted == messages
