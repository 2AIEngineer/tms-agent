"""Tests de l'acheminement SSE de l'assistant par l'API.

Deux niveaux, et ils ne disent pas la même chose :

  - **l'acheminement** (`assistant_sse`) : mise en forme, maintien en vie, et
    la propriété la moins évidente — attendre l'événement suivant ne doit pas
    annuler le travail en cours. Un `wait_for` naïf perdrait une lecture du
    TMS à chaque battement de cœur ;
  - **les routes** : mêmes garde-fous que la voie non diffusée, et un corps
    SSE réellement émis.

Le transport ASGI de httpx rassemble la réponse entière avant de la rendre : il
convient ici parce que ce flux **se termine** — contrairement au flux
d'alertes, qui est sans fin et se teste autrement.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from services.ops_api.auth import principal_from_claims
from services.ops_api.config import OpsApiSettings
from services.ops_api.dependencies import AppState, principal
from services.ops_api.main import build_app
from services.ops_api.repository import OpsRepository
from services.ops_api.streaming import assistant_sse
from shared.mapped_dto import (
    ActionDecision,
    AssistantAnswer,
    AssistantEvent,
    AssistantMode,
    StreamDone,
    StreamStarted,
    TextDelta,
)

pytestmark = pytest.mark.integration

OPERATOR = "11111111-2222-3333-4444-555555555555"


class StreamingStubAssistant:
    """Assistant scripté : les tests des routes portent sur les routes."""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.requests: list[object] = []
        self.decisions: list[tuple[uuid.UUID, str, ActionDecision]] = []
        self._delay = delay

    def _events(self, conversation_id: uuid.UUID) -> list[AssistantEvent]:
        answer = AssistantAnswer(
            conversation_id=conversation_id,
            answer="Deux missions en cours.",
            mode=AssistantMode.ASK,
            answered_at=datetime.now(UTC),
        )
        return [
            StreamStarted(
                conversation_id=conversation_id,
                mode=AssistantMode.ASK,
                allow_actions=False,
                started_at=datetime.now(UTC),
            ),
            TextDelta(text="Deux missions "),
            TextDelta(text="en cours."),
            StreamDone(answer=answer),
        ]

    async def stream(self, request) -> AsyncIterator[AssistantEvent]:
        self.requests.append(request)
        for event in self._events(uuid.uuid4()):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield event

    async def decide_stream(self, action_id, *, operator_id, decision) -> AsyncIterator[AssistantEvent]:
        self.decisions.append((action_id, operator_id, decision))
        for event in self._events(uuid.uuid4()):
            yield event


def make_app(database, *, roles=("ops_agent",), assistant=None):
    settings = OpsApiSettings(auth_disabled=False, sse_keepalive_s=0.05)
    app = build_app(settings)
    app.state.ops = AppState(
        settings=settings,
        database=database,
        repository=OpsRepository(),
        assistant=assistant,
    )
    if roles is not None:
        app.dependency_overrides[principal] = lambda: principal_from_claims(
            {"oid": OPERATOR, "roles": list(roles)}
        )
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


class TestRouteDeConversation:
    async def test_corps_sse_et_en_tetes(self, database):
        stub = StreamingStubAssistant()
        app = make_app(database, assistant=stub)

        async with client(app) as http:
            response = await http.post("/assistant/stream", json={"message": "Combien de missions ?"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Sans cet en-tête, un mandataire met le flux en tampon et le temps
        # réel disparaît — ce que ce flux existe précisément pour apporter.
        assert response.headers["x-accel-buffering"] == "no"
        corps = response.text
        assert "event: started" in corps
        assert "event: text_delta" in corps
        assert corps.rstrip().endswith("}")
        assert "event: done" in corps

    async def test_identite_prise_dans_le_jeton(self, database):
        """Diffuser ne doit pas ouvrir une porte que l'autre route ferme."""
        stub = StreamingStubAssistant()
        app = make_app(database, assistant=stub)

        async with client(app) as http:
            await http.post("/assistant/stream", json={"message": "bonjour"})

        assert stub.requests[0].operator_id == OPERATOR

    async def test_agir_exige_le_role(self, database):
        stub = StreamingStubAssistant()
        app = make_app(database, roles=("ops_viewer",), assistant=stub)

        async with client(app) as http:
            response = await http.post(
                "/assistant/stream", json={"message": "Clos l'alerte", "allow_actions": True}
            )

        assert response.status_code == 403
        assert not stub.requests

    async def test_sans_jeton_refuse(self, database):
        app = make_app(database, roles=None, assistant=StreamingStubAssistant())

        async with client(app) as http:
            response = await http.post("/assistant/stream", json={"message": "bonjour"})

        assert response.status_code == 401

    async def test_champ_inattendu_refuse(self, database):
        """Le contrat reste strict sur la voie diffusée."""
        app = make_app(database, assistant=StreamingStubAssistant())

        async with client(app) as http:
            response = await http.post(
                "/assistant/stream", json={"message": "bonjour", "operator_id": "ops_autre"}
            )

        assert response.status_code == 422

    async def test_assistant_indisponible(self, database):
        """Sans serveur MCP ni inférence, la route le dit franchement."""
        app = make_app(database, assistant=None)

        async with client(app) as http:
            response = await http.post("/assistant/stream", json={"message": "bonjour"})

        assert response.status_code == 503


class TestRoutesDeValidation:
    async def test_validation_diffusee(self, database):
        stub = StreamingStubAssistant()
        app = make_app(database, roles=("ops_supervisor",), assistant=stub)
        action_id = uuid.uuid4()

        async with client(app) as http:
            response = await http.post(f"/assistant/actions/{action_id}/confirm/stream")

        assert response.status_code == 200
        assert "event: done" in response.text
        assert stub.decisions == [(action_id, OPERATOR, ActionDecision.CONFIRM)]

    async def test_refus_diffuse(self, database):
        stub = StreamingStubAssistant()
        app = make_app(database, assistant=stub)
        action_id = uuid.uuid4()

        async with client(app) as http:
            await http.post(f"/assistant/actions/{action_id}/reject/stream")

        assert stub.decisions == [(action_id, OPERATOR, ActionDecision.REJECT)]

    async def test_validation_exige_le_role(self, database):
        stub = StreamingStubAssistant()
        app = make_app(database, roles=("ops_viewer",), assistant=stub)

        async with client(app) as http:
            response = await http.post(f"/assistant/actions/{uuid.uuid4()}/confirm/stream")

        assert response.status_code == 403
        assert not stub.decisions


class TestAcheminement:
    async def test_maintien_en_vie_pendant_un_silence(self):
        """Sans trafic, un mandataire coupe une connexion inactive."""

        async def lent() -> AsyncIterator[AssistantEvent]:
            await asyncio.sleep(0.25)
            yield TextDelta(text="enfin")

        morceaux = [chunk async for chunk in assistant_sse(lent(), keepalive_s=0.05)]

        assert morceaux.count(": keepalive\n\n") >= 2
        assert morceaux[-1].startswith("event: text_delta")

    async def test_lattente_nannule_pas_le_travail_en_cours(self):
        """La propriété la moins évidente de ce module.

        Un `wait_for` naïf annulerait, au délai écoulé, l'étape que le
        générateur avait commencée : une lecture du TMS perdue à chaque
        battement de cœur. La tâche doit être conservée d'une attente à
        l'autre.
        """
        etapes: list[str] = []

        async def travail() -> AsyncIterator[AssistantEvent]:
            etapes.append("début")
            await asyncio.sleep(0.2)
            etapes.append("fin")
            yield TextDelta(text="résultat")

        morceaux = [chunk async for chunk in assistant_sse(travail(), keepalive_s=0.05)]

        assert etapes == ["début", "fin"]
        assert any("résultat" in chunk for chunk in morceaux)

    async def test_incident_rendu_comme_evenement(self):
        """Une interface distingue mal une fermeture d'un silence."""

        async def casse() -> AsyncIterator[AssistantEvent]:
            yield TextDelta(text="avant")
            raise RuntimeError("base indisponible")

        morceaux = [chunk async for chunk in assistant_sse(casse(), keepalive_s=1.0)]

        assert "event: error" in morceaux[-1]
        assert "base indisponible" in morceaux[-1]

    async def test_abandon_ferme_le_generateur(self):
        """Fermer le flux doit faire exécuter les `finally` du service."""
        ferme = asyncio.Event()

        async def surveille() -> AsyncIterator[AssistantEvent]:
            try:
                yield TextDelta(text="un")
                yield TextDelta(text="deux")
            finally:
                ferme.set()

        flux = assistant_sse(surveille(), keepalive_s=1.0)
        premier = await flux.__anext__()
        await flux.aclose()

        assert premier.startswith("event: text_delta")
        assert ferme.is_set()

    async def test_type_porte_par_le_nom_et_la_charge(self):
        """Un client par nom d'événement et un client par canal unique."""

        async def un_evenement() -> AsyncIterator[AssistantEvent]:
            yield TextDelta(text="bonjour")

        morceaux = [chunk async for chunk in assistant_sse(un_evenement(), keepalive_s=1.0)]

        nom, _, charge = morceaux[0].partition("\ndata: ")
        assert nom == "event: text_delta"
        assert json.loads(charge)["type"] == "text_delta"
