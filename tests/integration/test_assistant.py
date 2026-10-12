"""Tests de l'assistant opérateur (F24 à F26).

Trois propriétés décident de la confiance qu'un exploitant peut lui accorder,
et ce sont elles qui sont éprouvées ici :

  - **l'historique survit** au redémarrage du service, puisqu'il est en base ;
  - **il n'agit que sur autorisation explicite.** Sans elle, les outils
    d'action ne lui sont même pas présentés ;
  - **il n'émet ni alerte ni notification**, jamais, quelle que soit la
    demande.

Les lectures passent par le vrai serveur MCP branché sur le faux TMS : une
réponse d'assistant qui s'appuierait sur une forme de charge utile inventée ne
dirait rien de ce qui se passe en production.
"""

import uuid

import pytest
from sqlalchemy import func, select

from database.schema import (
    alerts,
    assistant_pending_actions,
    audit_log,
    conversation_messages,
    driver_calls,
    notification_outbox,
)
from services.agents.assistant.actions import AssistantActions
from services.agents.assistant.repository import ConversationRepository
from services.agents.assistant.service import UNAVAILABLE_ANSWER, AssistantService
from services.agents.assistant.tools import ASSISTANT_READ_TOOLS
from services.agents.integrations.mcp_client import AgentMcpClient
from services.agents.operations.driver_call import CallGuardrails, DriverCallService
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from services.monitoring.alerts.repository import AlertRepository
from shared.mapped_dto import (
    ActionDecision,
    ActionKind,
    Alert,
    AlertStatus,
    AssistantMode,
    AssistantRequest,
    AssistantStatus,
    MessageRole,
    ResolutionReason,
    Severity,
)
from tests.factories import MISSION_ID, TMS_NOW
from tests.fake_llm import FakeLlm, reply, tool_call
from tests.fake_tms import FakeTms

pytestmark = pytest.mark.integration

OPERATOR = "ops_awa"


class RecordingCallChannel:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[object] = []

    @property
    def channel(self):
        from shared.mapped_dto import NotificationChannel

        return NotificationChannel.CALL

    async def send(self, message, *, sent_at):
        from shared.channels import SendResult

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


@pytest.fixture
def channel() -> RecordingCallChannel:
    return RecordingCallChannel()


def build(database, mcp, llm: FakeLlm, *, channel=None, **kwargs) -> AssistantService:
    calls = DriverCallService(channel, guardrails=CallGuardrails()) if channel else None
    actions = AssistantActions(database, mcp, driver_calls=calls)
    return AssistantService(llm, mcp, actions, database=database, **kwargs)  # type: ignore[arg-type]


def ask(message: str = "Où en est la mission msn_1 ?", **overrides) -> AssistantRequest:
    base = {
        "operator_id": OPERATOR,
        "message": message,
        "mode": AssistantMode.ASK,
        "tms_ids": ("x_tms",),
    }
    return AssistantRequest(**{**base, **overrides})


async def seed_alert(database, *, severity: Severity = Severity.HIGH) -> uuid.UUID:
    alert = Alert(
        tms_id="x_tms",
        mission_id=MISSION_ID,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=severity,
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


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


class TestReponse:
    async def test_question_ponctuelle(self, database, mcp):
        llm = FakeLlm([reply(content="Le véhicule roule, aucun écart signalé.")])

        answer = await build(database, mcp, llm).respond(ask())

        assert answer.answer.startswith("Le véhicule roule")
        assert answer.mode is AssistantMode.ASK
        assert answer.rounds == 1
        assert not answer.actions

    async def test_lecture_du_tms_citee_en_source(self, database, mcp):
        """Une réponse d'assistant doit pouvoir être vérifiée."""
        llm = FakeLlm(
            [
                reply(
                    calls=[tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": MISSION_ID})]
                ),
                reply(content="Mission en cours, écart de 0 km."),
            ]
        )

        answer = await build(database, mcp, llm).respond(ask())

        assert [source.tool for source in answer.sources] == ["get_mission_snapshot"]
        assert answer.rounds == 2

    async def test_contexte_de_linterface_transmis(self, database, mcp):
        """L'opérateur qui écrit depuis une fiche ne répète pas l'identifiant."""
        llm = FakeLlm([reply(content="Vu.")])

        await build(database, mcp, llm).respond(ask("Pourquoi est-elle en retard ?", mission_id="msn_1"))

        envoye = llm.calls[0]["messages"][-1]["content"]
        assert "msn_1" in envoye

    async def test_modele_indisponible_le_reste_fonctionne(self, database, mcp):
        """Sans LLM, l'assistant le dit — la détection n'en dépend pas."""
        from tests.fake_llm import unavailable

        llm = FakeLlm([unavailable()])

        answer = await build(database, mcp, llm).respond(ask())

        assert answer.unavailable
        assert answer.answer == UNAVAILABLE_ANSWER

    async def test_reponse_coupee_annoncee(self, database, mcp):
        """Mesuré en conditions réelles : le modèle local coupe ses rapports.

        Sans ce signalement, l'opérateur lit une phrase interrompue au milieu
        et ne peut pas savoir s'il manque l'essentiel.
        """
        from services.agents.assistant.service import TRUNCATION_NOTICE
        from services.agents.integrations.litellm_client import LlmReply, Usage

        llm = FakeLlm(
            [
                LlmReply(
                    content="Mission MIS-2026-00001 : écart de 7,9 km depuis 5 min. Dernière vue 2026-10-10T",
                    model="faux-modele",
                    usage=Usage(input_tokens=11280, output_tokens=224),
                    finish_reason="length",
                )
            ]
        )

        answer = await build(database, mcp, llm).respond(ask(mode=AssistantMode.REPORT))

        assert answer.truncated
        assert answer.answer.endswith(TRUNCATION_NOTICE)


class TestHistoire:
    async def test_historique_persiste_et_relu(self, database, mcp):
        """F25 : un second service, comme après un redémarrage, retrouve tout."""
        first = await build(database, mcp, FakeLlm([reply(content="Elle arrive à 14h.")])).respond(
            ask("Quand arrive msn_1 ?", mode=AssistantMode.CHAT)
        )

        # Nouveau service, nouveau client : plus rien en mémoire.
        llm = FakeLlm([reply(content="Toujours 14h.")])
        second = await build(database, mcp, llm).respond(
            ask("Et maintenant ?", mode=AssistantMode.CHAT, conversation_id=first.conversation_id)
        )

        assert second.conversation_id == first.conversation_id
        relus = [message["content"] for message in llm.calls[0]["messages"]]
        assert "Quand arrive msn_1 ?" in relus
        assert "Elle arrive à 14h." in relus

    async def test_les_lectures_sont_conservees(self, database, mcp):
        """Une réponse reste vérifiable après coup, donc les lectures aussi."""
        llm = FakeLlm(
            [
                reply(calls=[tool_call("get_tms_time", {"tms_id": "x_tms"})]),
                reply(content="Il est 9h au TMS."),
            ]
        )
        service = build(database, mcp, llm)

        answer = await service.respond(ask())
        messages = await service.history(answer.conversation_id, OPERATOR)

        roles = [message.role for message in messages]
        assert roles == [MessageRole.OPERATOR, MessageRole.TOOL, MessageRole.ASSISTANT]
        assert messages[1].tool_name == "get_tms_time"

    async def test_conversation_dun_collegue_non_poursuivie(self, database, mcp):
        """Un identifiant deviné ne doit pas ouvrir l'historique d'un autre."""
        sienne = await build(database, mcp, FakeLlm([reply(content="…")])).respond(
            ask("Ma question confidentielle", mode=AssistantMode.CHAT)
        )

        llm = FakeLlm([reply(content="Bonjour.")])
        autre = await build(database, mcp, llm).respond(
            ask("Bonjour ?", operator_id="ops_autre", conversation_id=sienne.conversation_id)
        )

        assert autre.conversation_id != sienne.conversation_id
        relus = " ".join(str(message["content"]) for message in llm.calls[0]["messages"])
        assert "confidentielle" not in relus

    async def test_historique_borne(self, database, mcp):
        """L'historique entier est renvoyé à chaque tour : il doit être plafonné."""
        service = build(database, mcp, FakeLlm([reply(content="ok")]), history_limit=4)
        conversation = await service.respond(ask("premier", mode=AssistantMode.CHAT))
        for index in range(5):
            service = build(database, mcp, FakeLlm([reply(content=f"r{index}")]), history_limit=4)
            await service.respond(
                ask(
                    f"question {index}", mode=AssistantMode.CHAT, conversation_id=conversation.conversation_id
                )
            )

        llm = FakeLlm([reply(content="dernier")])
        await build(database, mcp, llm, history_limit=4).respond(
            ask("et après ?", mode=AssistantMode.CHAT, conversation_id=conversation.conversation_id)
        )

        # Prompt système + 4 messages d'historique + question courante.
        assert len(llm.calls[0]["messages"]) == 6
        relus = " ".join(str(message["content"]) for message in llm.calls[0]["messages"])
        assert "premier" not in relus


class TestActions:
    async def test_sans_autorisation_aucun_outil_daction(self, database, mcp):
        """F26 : la permission d'agir s'accorde demande par demande."""
        llm = FakeLlm([reply(content="Je ne peux pas clore sans autorisation.")])

        await build(database, mcp, llm).respond(ask("Clos l'alerte."))

        proposes = llm.calls[0]["tools"]
        assert all(kind.value not in proposes for kind in ActionKind)

    async def test_action_inventee_refusee(self, database, mcp):
        """Un modèle peut inventer un nom d'outil : le refus doit être explicite."""
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(calls=[tool_call("close_alert", {"alert_id": str(alert_id), "reason": "il le faut"})]),
                reply(content="Je n'ai pas pu la clore."),
            ]
        )

        answer = await build(database, mcp, llm).respond(ask("Clos l'alerte."))

        assert not answer.actions
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value

    async def test_cloture_proposee_puis_validee(self, database, mcp):
        """Le geste est préparé, montré, puis exécuté tel quel.

        C'est le cœur du dispositif : l'opérateur valide **une ligne**, pas une
        intention. Entre la proposition et l'exécution, le modèle ne repasse
        pas et ne peut donc pas substituer une autre alerte.
        """
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {
                                "alert_id": str(alert_id),
                                "reason": "Détour autorisé par le client, confirmé par téléphone.",
                            },
                        )
                    ]
                ),
                reply(content="Je propose de clore cette alerte : validez-vous ?"),
            ]
        )
        service = build(database, mcp, llm)

        propose = await service.respond(ask("Clos cette alerte.", allow_actions=True))

        assert propose.status is AssistantStatus.AWAITING_CONFIRMATION
        assert not propose.actions
        pending = propose.pending_actions[0]
        # La description est construite par le code : l'opérateur lit ce qui
        # va réellement s'exécuter, pas la prose du modèle.
        assert "Dérive d'itinéraire" in pending.summary
        assert MISSION_ID in pending.summary
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value

        # L'exécution n'a lieu qu'à la validation, et le modèle en rend compte.
        service = build(database, mcp, FakeLlm([reply(content="C'est fait, alerte close.")]))
        confirm = await service.decide(pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM)

        assert [action.kind for action in confirm.actions] == [ActionKind.CLOSE_ALERT]
        assert confirm.actions[0].succeeded
        async with database.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(alerts.c.status, alerts.c.resolution_reason).where(alerts.c.id == alert_id)
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == AlertStatus.RESOLVED.value
        # `operator` et non `condition_cleared` : la condition tenait peut-être
        # encore, et confondre les deux tromperait la relecture.
        assert row["resolution_reason"] == ResolutionReason.OPERATOR.value

    async def test_refus_nexecute_rien(self, database, mcp):
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {"alert_id": str(alert_id), "reason": "Fausse alerte, semble-t-il."},
                        )
                    ]
                ),
                reply(content="Je propose de clore. Validez-vous ?"),
            ]
        )
        service = build(database, mcp, llm)
        propose = await service.respond(ask("Clos-la.", allow_actions=True))

        refus = await service.decide(
            propose.pending_actions[0].id, operator_id=OPERATOR, decision=ActionDecision.REJECT
        )

        assert "annulée" in refus.answer
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value

    async def test_cloture_journalisee_avec_son_demandeur(self, database, mcp):
        """F27 : « l'assistant a clos » ne suffit pas, il faut savoir qui a validé."""
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {"alert_id": str(alert_id), "reason": "Fausse alerte confirmée au téléphone."},
                        )
                    ]
                ),
                reply(content="Je propose de clore."),
            ]
        )
        service = build(database, mcp, llm)
        propose = await service.respond(ask("Clos-la.", allow_actions=True))

        service = build(database, mcp, FakeLlm([reply(content="Fait.")]))
        await service.decide(
            propose.pending_actions[0].id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        async with database.connection() as connection:
            entry = (
                (
                    await connection.execute(
                        select(audit_log.c.action, audit_log.c.actor, audit_log.c.detail).where(
                            audit_log.c.alert_id == alert_id
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert entry["action"] == "alert_closed_by_operator"
        assert entry["actor"] == OPERATOR
        assert "téléphone" in str(entry["detail"]["reason"])

    async def test_alerte_deja_close_ne_derange_pas_loperateur(self, database, mcp):
        """Inutile de faire valider un geste sans objet."""
        alert_id = await seed_alert(database)
        async with database.transaction() as connection:
            await AlertRepository().resolve(
                connection, [alert_id], resolved_at=TMS_NOW, reason=ResolutionReason.CONDITION_CLEARED
            )
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {"alert_id": str(alert_id), "reason": "Demande de l'exploitation, déjà traitée."},
                        )
                    ]
                ),
                reply(content="Elle était déjà close."),
            ]
        )

        answer = await build(database, mcp, llm).respond(ask("Clos-la.", allow_actions=True))

        assert answer.status is AssistantStatus.ANSWERED
        assert not answer.pending_actions
        assert await count(database, assistant_pending_actions) == 0

    async def test_appel_chauffeur_sous_les_memes_quotas(self, database, mcp, channel):
        """Les quotas sont partagés avec l'enquêteur : le chauffeur est unique."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "call_driver",
                            {
                                "tms_id": "x_tms",
                                "mission_id": MISSION_ID,
                                "reason": "Demander la raison de l'arrêt prolongé.",
                            },
                        )
                    ]
                ),
                reply(content="Je propose d'appeler le chauffeur."),
            ]
        )
        service = build(database, mcp, llm, channel=channel)
        propose = await service.respond(ask("Appelle le chauffeur.", allow_actions=True))

        # Le nom et le numéro figurent dans la proposition : valider un appel
        # suppose de savoir qui va être dérangé.
        assert "+212" in propose.pending_actions[0].summary or "+33" in propose.pending_actions[0].summary
        assert not channel.sent

        service = build(database, mcp, FakeLlm([reply(content="Appel passé.")]), channel=channel)
        confirm = await service.decide(
            propose.pending_actions[0].id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert confirm.actions[0].kind is ActionKind.CALL_DRIVER
        assert confirm.actions[0].succeeded
        assert len(channel.sent) == 1
        async with database.connection() as connection:
            row = (
                (await connection.execute(select(driver_calls.c.requested_by, driver_calls.c.status)))
                .mappings()
                .one()
            )
        assert row["requested_by"] == "assistant"
        assert row["status"] == "placed"

    async def test_motif_trop_court_refuse(self, database, mcp):
        alert_id = await seed_alert(database)
        llm = FakeLlm(
            [
                reply(calls=[tool_call("close_alert", {"alert_id": str(alert_id), "reason": "ok"})]),
                reply(content="Il me faut un motif."),
            ]
        )

        answer = await build(database, mcp, llm).respond(ask("Clos-la.", allow_actions=True))

        assert not answer.actions

    async def test_sans_telephonie_loutil_disparait(self, database, mcp):
        """`DRIVER_CALL_ENABLED=false` : mieux vaut un outil absent qu'un refus."""
        llm = FakeLlm([reply(content="La téléphonie est coupée.")])

        await build(database, mcp, llm).respond(ask("Appelle-le.", allow_actions=True))

        assert "call_driver" not in llm.calls[0]["tools"]
        assert "close_alert" in llm.calls[0]["tools"]

    async def test_decision_consignee_sans_rien_changer(self, database, mcp):
        """« Détour autorisé, aucune action » est une information."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "log_decision",
                            {
                                "tms_id": "x_tms",
                                "mission_id": MISSION_ID,
                                "reason": "Détour autorisé par le client, aucune action à prendre.",
                            },
                        )
                    ]
                ),
                reply(content="C'est consigné."),
            ]
        )

        answer = await build(database, mcp, llm).respond(
            ask("Note que le détour est autorisé.", allow_actions=True)
        )

        assert answer.actions[0].kind is ActionKind.LOG_DECISION
        async with database.connection() as connection:
            actions = [
                row["action"] for row in (await connection.execute(select(audit_log.c.action))).mappings()
            ]
        assert actions == ["operator_decision"]


class TestCeQuilNeFaitJamais:
    async def test_aucun_outil_de_notification(self, database, mcp):
        """F26 : l'assistant ne notifie personne, même autorisé à agir."""
        llm = FakeLlm([reply(content="…")])

        await build(database, mcp, llm).respond(ask(allow_actions=True))

        proposes = llm.calls[0]["tools"]
        assert "resolve_recipients" not in proposes
        assert not any("notif" in name for name in proposes)

    async def test_aucune_notification_ni_alerte_creee(self, database, mcp, channel):
        """Quoi qu'il fasse, il n'ajoute ni alerte ni envoi dans la file."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "log_decision",
                            {"tms_id": "x_tms", "reason": "Rien à signaler sur cette tournée."},
                        )
                    ]
                ),
                reply(content="Consigné."),
            ]
        )

        await build(database, mcp, llm, channel=channel).respond(
            ask("Fais le point.", mode=AssistantMode.REPORT, allow_actions=True)
        )

        assert await count(database, alerts) == 0
        assert await count(database, notification_outbox) == 0


class TestRepositoryDesConversations:
    async def test_rangs_successifs(self, database):
        """Le rang vient de la base : deux messages ne peuvent pas se confondre."""
        repository = ConversationRepository()
        async with database.transaction() as connection:
            conversation_id = await repository.open(
                connection, operator_id=OPERATOR, title="t", mode=AssistantMode.CHAT
            )
        from shared.mapped_dto import ConversationMessage

        for index in range(3):
            async with database.transaction() as connection:
                await repository.append(
                    connection,
                    conversation_id,
                    [ConversationMessage(role=MessageRole.OPERATOR, content=f"m{index}")],
                )

        async with database.connection() as connection:
            seqs = [
                row["seq"]
                for row in (
                    await connection.execute(
                        select(conversation_messages.c.seq).order_by(conversation_messages.c.seq)
                    )
                ).mappings()
            ]
        assert seqs == [1, 2, 3]

    async def test_conversations_recentes_dabord(self, database, mcp):
        service = build(database, mcp, FakeLlm([reply(content="a")]))
        await service.respond(ask("première", mode=AssistantMode.CHAT))
        service = build(database, mcp, FakeLlm([reply(content="b")]))
        await service.respond(ask("seconde", mode=AssistantMode.CHAT))

        async with database.connection() as connection:
            recentes = await ConversationRepository().recent(connection, OPERATOR)

        assert [str(item["title"]) for item in recentes] == ["seconde", "première"]
