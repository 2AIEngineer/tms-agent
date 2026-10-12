"""Tests de l'inférence diffusée.

Le protocole de diffusion compatible OpenAI a deux pièges, et ce sont eux qui
sont éprouvés ici — pas la mécanique HTTP :

  - **les appels d'outils arrivent fragmentés.** Le nom dans un fragment, les
    arguments dans plusieurs autres, parfois coupés au milieu d'une chaîne
    JSON. Concaténer dans l'ordre d'arrivée mélangerait deux outils demandés
    d'un coup, d'où l'accumulation par `index` ;
  - **la consommation de jetons n'arrive qu'à la fin.** Sans elle, le poste de
    coût principal du système deviendrait invisible dès qu'on diffuse.

Un troisième point tient à notre doctrine : le raisonnement n'est relayé que
si le modèle en envoie. Rien n'est fabriqué.
"""

import json

import httpx
import pytest

from services.agents.integrations.litellm_client import (
    LiteLlmClient,
    LlmCallFailed,
    LlmErrorKind,
)


def sse(*events: dict) -> bytes:
    """Fabrique un flux tel que le proxy l'émet, `[DONE]` compris."""
    body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events)
    return (body + "data: [DONE]\n\n").encode()


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields, "finish_reason": None}]}


def finish(reason: str = "stop", usage: dict | None = None) -> dict:
    event: dict = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    if usage is not None:
        event["usage"] = usage
    return event


def client_for(payload: bytes, *, status: int = 200) -> LiteLlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload)

    transport = httpx.MockTransport(handler)
    return LiteLlmClient(
        "http://proxy.test",
        "clef",
        model="mission-investigator",
        client=httpx.AsyncClient(transport=transport, base_url="http://proxy.test"),
    )


async def collect(client: LiteLlmClient):
    fragments = []
    reply = None
    async for chunk in client.complete_stream([{"role": "user", "content": "bonjour"}]):
        if chunk.reply is not None:
            reply = chunk.reply
        else:
            fragments.append(chunk)
    return fragments, reply


class TestTexteDiffuse:
    async def test_fragments_puis_reponse_complete(self):
        client = client_for(
            sse(
                delta(content="Le véhicule "),
                delta(content="roule "),
                delta(content="normalement."),
                finish(usage={"prompt_tokens": 120, "completion_tokens": 8}),
            )
        )

        fragments, reply = await collect(client)

        assert [fragment.text for fragment in fragments] == [
            "Le véhicule ",
            "roule ",
            "normalement.",
        ]
        assert reply is not None
        # Le dernier fragment porte exactement ce que `complete` aurait rendu.
        assert reply.content == "Le véhicule roule normalement."
        assert reply.finish_reason == "stop"
        await client.aclose()

    async def test_jetons_comptes_malgre_la_diffusion(self):
        """Le coût est le poste principal du système : il doit rester mesuré."""
        client = client_for(
            sse(
                delta(content="ok"),
                finish(usage={"prompt_tokens": 1200, "completion_tokens": 42}),
            )
        )

        _, reply = await collect(client)

        assert reply is not None
        assert reply.usage.input_tokens == 1200
        assert reply.usage.output_tokens == 42
        await client.aclose()

    async def test_coupure_par_longueur_rapportee(self):
        client = client_for(sse(delta(content="une phrase inter"), finish("length")))

        _, reply = await collect(client)

        assert reply is not None
        assert reply.finish_reason == "length"
        await client.aclose()


class TestAppelsDoutilsFragmentes:
    async def test_arguments_recomposes(self):
        """Les arguments arrivent coupés, parfois au milieu d'une chaîne."""
        client = client_for(
            sse(
                delta(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "get_mission_snapshot", "arguments": ""},
                        }
                    ]
                ),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '{"tms_id": "x_t'}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": 'ms", "mission_'}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": 'id": "msn_1"}'}}]),
                finish("tool_calls"),
            )
        )

        _, reply = await collect(client)

        assert reply is not None
        assert reply.wants_tools
        call = reply.tool_calls[0]
        assert call.name == "get_mission_snapshot"
        assert call.arguments == {"tms_id": "x_tms", "mission_id": "msn_1"}
        assert call.id == "call_1"
        await client.aclose()

    async def test_deux_outils_entrelaces(self):
        """Accumuler dans l'ordre d'arrivée mélangerait leurs arguments."""
        client = client_for(
            sse(
                delta(
                    tool_calls=[
                        {"index": 0, "id": "a", "function": {"name": "get_driver", "arguments": ""}},
                        {"index": 1, "id": "b", "function": {"name": "get_vehicle", "arguments": ""}},
                    ]
                ),
                delta(tool_calls=[{"index": 1, "function": {"arguments": '{"vehicle_id":'}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": '{"driver_id":'}}]),
                delta(tool_calls=[{"index": 1, "function": {"arguments": ' "veh_1"}'}}]),
                delta(tool_calls=[{"index": 0, "function": {"arguments": ' "drv_1"}'}}]),
                finish("tool_calls"),
            )
        )

        _, reply = await collect(client)

        assert reply is not None
        noms = {call.name: call.arguments for call in reply.tool_calls}
        assert noms["get_driver"] == {"driver_id": "drv_1"}
        assert noms["get_vehicle"] == {"vehicle_id": "veh_1"}
        await client.aclose()

    async def test_arguments_illisibles_ne_perdent_pas_le_tour(self):
        """Un JSON invalide est rendu vide : l'outil répondra une erreur."""
        client = client_for(
            sse(
                delta(
                    tool_calls=[
                        {"index": 0, "id": "a", "function": {"name": "get_driver", "arguments": "{oops"}}
                    ]
                ),
                finish("tool_calls"),
            )
        )

        _, reply = await collect(client)

        assert reply is not None
        assert reply.tool_calls[0].arguments == {}
        assert reply.tool_calls[0].raw_arguments == "{oops"
        await client.aclose()


class TestRaisonnement:
    async def test_relaye_sil_est_emis(self):
        client = client_for(
            sse(
                delta(reasoning_content="Je vérifie l'écart. "),
                delta(content="Écart de 7 km."),
                finish(),
            )
        )

        fragments, reply = await collect(client)

        assert [f.reasoning for f in fragments if f.reasoning] == ["Je vérifie l'écart. "]
        assert reply is not None
        assert reply.reasoning == "Je vérifie l'écart. "
        await client.aclose()

    async def test_rien_nest_fabrique_sans_lui(self):
        """La plupart des modèles n'en émettent pas : le champ reste vide."""
        client = client_for(sse(delta(content="Écart de 7 km."), finish()))

        fragments, reply = await collect(client)

        assert all(not fragment.reasoning for fragment in fragments)
        assert reply is not None
        assert reply.reasoning is None
        await client.aclose()


class TestRobustesse:
    async def test_fragment_illisible_ignore(self):
        """Un octet perdu ne doit pas emporter la conversation."""
        payload = (
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            b"data: {ceci nest pas du json\n\n"
            b"data: [DONE]\n\n"
        )
        client = client_for(payload)

        fragments, reply = await collect(client)

        assert [fragment.text for fragment in fragments] == ["a"]
        assert reply is not None
        await client.aclose()

    async def test_quota_qualifie_comme_indisponibilite(self):
        """Même qualification que sur le chemin non diffusé."""
        client = client_for(b'{"error": {"message": "rate limit"}}', status=429)

        with pytest.raises(LlmCallFailed) as refus:
            await collect(client)

        assert refus.value.kind is LlmErrorKind.UNAVAILABLE
        assert refus.value.skip_investigation
        await client.aclose()

    async def test_sans_cle_aucun_appel_reseau(self):
        client = LiteLlmClient("http://proxy.test", "", model="m")

        with pytest.raises(LlmCallFailed) as refus:
            await collect(client)

        assert refus.value.kind is LlmErrorKind.UNAVAILABLE
        await client.aclose()


class TestFenetreDeContexte:
    """Un dépassement de fenêtre n'est ni une panne ni un défaut irrécupérable.

    Rencontré en exécution réelle : le fournisseur refuse en **400**, code que
    l'on classerait « défaut définitif » — et l'enquête serait perdue alors
    qu'un historique compacté passerait. La qualification se fait donc sur le
    message, seul à distinguer « trop long » de « requête malformée ».
    """

    async def test_message_du_fournisseur_reconnu(self):
        from services.agents.integrations.litellm_client import LiteLlmClient as Client

        detail = (
            "litellm.ContextWindowExceededError: This model's maximum context length "
            "is 4096 tokens. However, you requested 0 output tokens and your prompt "
            "contains at least 4097 input tokens."
        )
        client = client_for(json.dumps({"error": {"message": detail}}).encode(), status=400)

        with pytest.raises(LlmCallFailed) as refus:
            await collect(client)

        assert refus.value.kind is LlmErrorKind.CONTEXT_OVERFLOW
        assert refus.value.context_overflow is True
        # Ni « sauté » ni « à réessayer tel quel » : l'appelant doit compacter.
        assert refus.value.skip_investigation is False
        assert refus.value.retryable is False
        assert isinstance(client, Client)
        await client.aclose()

    async def test_refus_du_repli_reconnu_aussi(self):
        """LiteLLM concatène les deux échecs : reconnaître l'un suffit."""
        detail = (
            "model group 'mission-investigator' failed. Fallback also failed: "
            "RateLimitError: Request too large, please reduce your message size"
        )
        client = client_for(json.dumps({"error": {"message": detail}}).encode(), status=400)

        with pytest.raises(LlmCallFailed) as refus:
            await collect(client)

        assert refus.value.kind is LlmErrorKind.CONTEXT_OVERFLOW
        await client.aclose()

    async def test_autre_requete_malformee_reste_definitive(self):
        """Un 400 qui n'est pas un dépassement ne doit pas être réessayé."""
        client = client_for(
            json.dumps({"error": {"message": "Invalid value for 'tool_choice'"}}).encode(),
            status=400,
        )

        with pytest.raises(LlmCallFailed) as refus:
            await collect(client)

        assert refus.value.kind is LlmErrorKind.PERMANENT
        await client.aclose()
