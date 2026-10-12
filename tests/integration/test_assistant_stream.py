"""Tests du flux temps réel de l'assistant.

Ce que ce module éprouve n'est pas « est-ce que des événements sortent », mais
les deux propriétés qui décident si l'on peut s'appuyer dessus :

  - **un seul chemin de code.** Diffuser ou ne pas diffuser ne doit rien
    changer à ce qui est écrit en base, aux garde-fous, ni à la validation
    humaine. Les tests comparent donc l'état de la base après les deux
    chemins, plutôt que de faire confiance à la lecture du code ;
  - **ce qui a eu lieu reste écrit**, même si l'opérateur ferme son onglet au
    milieu. Une écriture unique en fin de tour aurait tout perdu, y compris sa
    question.

L'ordre des événements est vérifié parce qu'une interface s'y appuie :
`started` arrive avant tout jeton, et une action proposée est annoncée avant
`done` — sans quoi le bouton de validation n'apparaîtrait qu'à la fin.
"""

import uuid

import pytest
from sqlalchemy import func, select

from database.schema import alerts, assistant_pending_actions, conversation_messages
from services.agents.assistant.actions import AssistantActions
from services.agents.assistant.service import AssistantService
from services.agents.assistant.tools import ASSISTANT_READ_TOOLS
from services.agents.integrations.mcp_client import AgentMcpClient
from services.agents.operations.driver_call import CallGuardrails, DriverCallService
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from services.monitoring.alerts.repository import AlertRepository
from shared.channels import SendResult
from shared.mapped_dto import (
    ActionDecision,
    Alert,
    AlertStatus,
    AssistantMode,
    AssistantRequest,
    AssistantStatus,
    MessageRole,
    NotificationChannel,
    PendingActionProposed,
    RoundStarted,
    Severity,
    StreamDone,
    StreamEventType,
    StreamStarted,
    TextDelta,
    ToolCompleted,
    ToolRole,
    ToolStarted,
)
from tests.factories import MISSION_ID, TMS_NOW
from tests.fake_llm import FakeLlm, reply, tool_call, unavailable
from tests.fake_tms import FakeTms

pytestmark = pytest.mark.integration

OPERATOR = "ops_awa"


class RecordingCallChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[object] = []

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.CALL

    async def send(self, message, *, sent_at):
        self.sent.append(message)
        return SendResult.ok(self.name, reference="ref_1")


@pytest.fixture
async def mcp():
    tms = FakeTms()
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with AgentMcpClient(server, allowed=ASSISTANT_READ_TOOLS) as client:
        yield client
    await context.aclose()


def build(database, mcp, llm: FakeLlm, *, channel=None) -> AssistantService:
    calls = DriverCallService(channel, guardrails=CallGuardrails()) if channel else None
    return AssistantService(
        llm,  # type: ignore[arg-type]
        mcp,
        AssistantActions(database, mcp, driver_calls=calls),
        database=database,
    )


def ask(message: str = "Où en est la mission msn_1 ?", **overrides) -> AssistantRequest:
    base = {
        "operator_id": OPERATOR,
        "message": message,
        "mode": AssistantMode.ASK,
        "tms_ids": ("x_tms",),
    }
    return AssistantRequest(**{**base, **overrides})


async def seed_alert(database) -> uuid.UUID:
    alert = Alert(
        tms_id="x_tms",
        mission_id=MISSION_ID,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=Severity.HIGH,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté.",
        deduplication_key=f"deviation:x_tms:{MISSION_ID}",
        detected_at=TMS_NOW,
    )
    async with database.transaction() as connection:
        alert_id = await AlertRepository().insert_alert(connection, alert)
    assert alert_id is not None
    return alert_id


async def drain(service: AssistantService, request: AssistantRequest) -> list:
    return [event async for event in service.stream(request)]


def types(events: list) -> list[str]:
    return [str(event.type) for event in events]


async def rows(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


class TestOrdreDesEvenements:
    async def test_identifiant_de_conversation_avant_tout_jeton(self, database, mcp):
        """L'interface doit pouvoir rattacher ce qui arrive à un fil."""
        llm = FakeLlm([reply(content="Le véhicule roule normalement, aucun écart.")])

        events = await drain(build(database, mcp, llm), ask())

        assert isinstance(events[0], StreamStarted)
        assert events[0].conversation_id is not None
        assert types(events)[1] == StreamEventType.ROUND_STARTED.value
        assert isinstance(events[-1], StreamDone)

    async def test_reponse_diffusee_en_fragments_puis_entiere(self, database, mcp):
        llm = FakeLlm([reply(content="Le véhicule roule normalement, aucun écart constaté.")])

        events = await drain(build(database, mcp, llm), ask())

        fragments = [event.text for event in events if isinstance(event, TextDelta)]
        assert len(fragments) > 1
        done = events[-1]
        assert isinstance(done, StreamDone)
        # La réponse entière est renvoyée : une interface peut se recaler sans
        # tenir de compteur, et un client non interactif n'écoute que `done`.
        assert "".join(fragments) == done.answer.answer

    async def test_lecture_annoncee_avant_et_apres(self, database, mcp):
        """Ce qui tient lieu de raisonnement visible : les étapes."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "get_mission_snapshot",
                            {"tms_id": "x_tms", "mission_id": MISSION_ID},
                        )
                    ]
                ),
                reply(content="Mission en cours, aucun écart."),
            ]
        )

        events = await drain(build(database, mcp, llm), ask())

        started = next(event for event in events if isinstance(event, ToolStarted))
        completed = next(event for event in events if isinstance(event, ToolCompleted))
        assert started.tool == "get_mission_snapshot"
        # Une lecture est anodine, une action engage : l'interface ne doit pas
        # les présenter de la même façon.
        assert started.role is ToolRole.READ
        assert started.arguments["mission_id"] == MISSION_ID
        assert completed.ok is True
        assert completed.duration_ms >= 0
        assert completed.preview
        # Les deux tours sont annoncés, avec le plafond : « 2 sur 6 » dit à
        # l'opérateur que le modèle approche de sa limite.
        tours = [event for event in events if isinstance(event, RoundStarted)]
        assert [tour.round for tour in tours] == [1, 2]
        assert tours[0].max_rounds == 6

    async def test_lecture_en_echec_rapportee(self, database, mcp):
        """Un outil inconnu : l'opérateur doit voir que la lecture a échoué."""
        llm = FakeLlm(
            [
                reply(calls=[tool_call("outil_inexistant", {})]),
                reply(content="Je n'ai pas pu lire cela."),
            ]
        )

        events = await drain(build(database, mcp, llm), ask())

        completed = next(event for event in events if isinstance(event, ToolCompleted))
        assert completed.ok is False

    async def test_action_annoncee_avant_la_fin(self, database, mcp):
        """Le bouton de validation doit apparaître sans attendre `done`."""
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {"alert_id": str(alert_id), "reason": "Détour autorisé par le client."},
                        )
                    ]
                ),
                reply(content="Je propose de clore cette alerte."),
            ]
        )

        events = await drain(build(database, mcp, llm), ask("Clos-la.", allow_actions=True))

        noms = types(events)
        assert StreamEventType.PENDING_ACTION.value in noms
        assert noms.index(StreamEventType.PENDING_ACTION.value) < noms.index(StreamEventType.DONE.value)
        proposee = next(event for event in events if isinstance(event, PendingActionProposed))
        assert "Dérive d'itinéraire" in proposee.action.summary
        # L'outil d'action est annoncé comme tel.
        started = next(event for event in events if isinstance(event, ToolStarted))
        assert started.role is ToolRole.ACTION
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.answer.status is AssistantStatus.AWAITING_CONFIRMATION

    async def test_modele_indisponible_annonce_dans_done(self, database, mcp):
        """« Indisponible » est un état, pas une connexion coupée."""
        events = await drain(build(database, mcp, FakeLlm([unavailable()])), ask())

        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.answer.status is AssistantStatus.UNAVAILABLE
        assert done.answer.unavailable


class TestUnSeulCheminDeCode:
    async def test_meme_etat_en_base_que_la_voie_non_diffusee(self, database, mcp):
        """Comparé, et non supposé : c'est la seule garantie qui tienne."""
        scenario = [
            reply(calls=[tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": MISSION_ID})]),
            reply(content="Mission en cours, aucun écart."),
        ]

        direct = await build(database, mcp, FakeLlm(list(scenario))).respond(ask())
        async with database.connection() as connection:
            apres_direct = [
                (row["role"], row["tool_name"])
                for row in (
                    await connection.execute(
                        select(conversation_messages.c.role, conversation_messages.c.tool_name)
                        .where(conversation_messages.c.conversation_id == direct.conversation_id)
                        .order_by(conversation_messages.c.seq)
                    )
                ).mappings()
            ]

        events = await drain(build(database, mcp, FakeLlm(list(scenario))), ask())
        diffuse = events[-1].answer
        async with database.connection() as connection:
            apres_flux = [
                (row["role"], row["tool_name"])
                for row in (
                    await connection.execute(
                        select(conversation_messages.c.role, conversation_messages.c.tool_name)
                        .where(conversation_messages.c.conversation_id == diffuse.conversation_id)
                        .order_by(conversation_messages.c.seq)
                    )
                ).mappings()
            ]

        assert apres_direct == apres_flux
        assert apres_flux == [
            (MessageRole.OPERATOR.value, None),
            (MessageRole.TOOL.value, "get_mission_snapshot"),
            (MessageRole.ASSISTANT.value, None),
        ]
        assert diffuse.answer == direct.answer

    async def test_garde_fou_daction_identique(self, database, mcp):
        """Sans autorisation, l'outil n'est pas même présenté — sur les deux voies."""
        llm = FakeLlm([reply(content="Je ne peux pas clore sans autorisation.")])

        await drain(build(database, mcp, llm), ask("Clos l'alerte."))

        assert "close_alert" not in llm.calls[0]["tools"]
        assert await rows(database, assistant_pending_actions) == 0

    async def test_jetons_comptes_sur_la_voie_diffusee(self, database, mcp):
        events = await drain(build(database, mcp, FakeLlm([reply(content="ok")])), ask())

        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.answer.input_tokens == 100
        assert done.answer.output_tokens == 40


class TestInterruption:
    async def test_question_et_lecture_conservees_si_le_flux_est_abandonne(self, database, mcp):
        """L'opérateur ferme son onglet pendant une lecture.

        Une écriture unique en fin de tour aurait tout perdu, y compris sa
        question — il croirait avoir mal cliqué et la reposerait.
        """
        llm = FakeLlm(
            [
                reply(
                    calls=[tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": MISSION_ID})]
                ),
                reply(content="Jamais lu : le client est parti avant."),
            ]
        )
        service = build(database, mcp, llm)
        stream = service.stream(ask())

        conservés = []
        async for event in stream:
            conservés.append(event)
            if isinstance(event, ToolCompleted):
                break
        await stream.aclose()

        async with database.connection() as connection:
            messages = [
                (row["role"], row["tool_name"])
                for row in (
                    await connection.execute(
                        select(conversation_messages.c.role, conversation_messages.c.tool_name).order_by(
                            conversation_messages.c.seq
                        )
                    )
                ).mappings()
            ]
        assert messages == [
            (MessageRole.OPERATOR.value, None),
            (MessageRole.TOOL.value, "get_mission_snapshot"),
        ]

    async def test_action_proposee_survit_a_labandon(self, database, mcp):
        """Elle attend dans la liste : l'opérateur la retrouve en revenant."""
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {"alert_id": str(alert_id), "reason": "Détour autorisé par le client."},
                        )
                    ]
                ),
                reply(content="Jamais lu."),
            ]
        )
        service = build(database, mcp, llm)
        stream = service.stream(ask("Clos-la.", allow_actions=True))

        async for event in stream:
            if isinstance(event, PendingActionProposed):
                break
        await stream.aclose()

        attente = await service.waiting_actions(OPERATOR)
        assert len(attente) == 1
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        # Rien n'a été exécuté : la validation reste à donner.
        assert status == AlertStatus.OPEN.value


class TestValidationDiffusee:
    async def test_execution_puis_suite_diffusees(self, database, mcp):
        """L'opérateur voit l'action partir, revenir, et la suite être proposée."""
        alert_id = await seed_alert(database)
        service = build(
            database,
            mcp,
            FakeLlm(
                [
                    reply(
                        calls=[
                            tool_call(
                                "close_alert",
                                {"alert_id": str(alert_id), "reason": "Détour autorisé par le client."},
                            )
                        ]
                    ),
                    reply(content="Je propose de clore."),
                ]
            ),
        )
        pending = (await service.respond(ask("Clos-la.", allow_actions=True))).pending_actions[0]

        suite = build(database, mcp, FakeLlm([reply(content="C'est fait, alerte close.")]))
        events = [
            event
            async for event in suite.decide_stream(
                pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
            )
        ]

        noms = types(events)
        assert noms[0] == StreamEventType.STARTED.value
        assert StreamEventType.TOOL_STARTED.value in noms
        assert StreamEventType.TOOL_COMPLETED.value in noms
        execution = next(event for event in events if isinstance(event, ToolCompleted))
        assert execution.tool == "close_alert"
        assert execution.role is ToolRole.ACTION
        assert execution.ok is True
        done = events[-1]
        assert isinstance(done, StreamDone)
        assert done.answer.actions[0].succeeded

    async def test_refus_diffuse_sans_appeler_le_modele(self, database, mcp):
        """Un refus est factuel : le faire rédiger par un LLM ajouterait du délai."""
        alert_id = await seed_alert(database)
        service = build(
            database,
            mcp,
            FakeLlm(
                [
                    reply(
                        calls=[
                            tool_call(
                                "close_alert",
                                {"alert_id": str(alert_id), "reason": "Peut-être une fausse alerte."},
                            )
                        ]
                    ),
                    reply(content="Je propose de clore."),
                ]
            ),
        )
        pending = (await service.respond(ask("Clos-la.", allow_actions=True))).pending_actions[0]

        muet = FakeLlm([])
        events = [
            event
            async for event in build(database, mcp, muet).decide_stream(
                pending.id, operator_id=OPERATOR, decision=ActionDecision.REJECT
            )
        ]

        assert muet.rounds == 0
        textes = [event.text for event in events if isinstance(event, TextDelta)]
        assert textes and "annulée" in textes[0]

    async def test_action_hors_perimetre_ne_diffuse_quun_done(self, database, mcp):
        events = [
            event
            async for event in build(database, mcp, FakeLlm([])).decide_stream(
                uuid.uuid4(), operator_id=OPERATOR, decision=ActionDecision.CONFIRM
            )
        ]

        assert types(events) == [StreamEventType.DONE.value]
