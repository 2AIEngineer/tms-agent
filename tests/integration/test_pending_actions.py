"""Tests de la validation humaine des actions de l'assistant.

Ce module éprouve la pièce qui rend l'assistant utilisable sur des gestes
irréversibles. Chaque test correspond à une façon précise dont un dispositif de
validation peut trahir celui qui s'y fie :

  - **substitution** : l'opérateur valide « clore l'alerte A » et c'est B qui
    est close. C'est ce qu'autorisait une autorisation portant sur un *tour* de
    conversation, le modèle replanifiant librement ;
  - **double exécution** : deux onglets, deux clics, deux appels au même
    chauffeur ;
  - **autorisation qui survit à sa situation** : un accord donné il y a deux
    heures exécuté maintenant ;
  - **mandat emprunté** : un collègue tranche une action qui ne lui était pas
    adressée ;
  - **silence après une panne** : un processus tué entre la validation et
    l'exécution, et une interface qui affiche « en cours » pour toujours.

Les transitions sont éprouvées sur PostgreSQL : l'unicité d'exécution repose
sur un `UPDATE … WHERE status = 'proposed' RETURNING`, et un double en mémoire
ne dirait rien de ce qui se passe sous concurrence réelle.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from database.schema import alerts, assistant_pending_actions, driver_calls
from services.agents.assistant.actions import AssistantActions
from services.agents.assistant.pending import (
    DEFAULT_TTL,
    REQUIRES_CONFIRMATION,
    PendingActionRepository,
)
from services.agents.assistant.repository import ConversationRepository
from services.agents.assistant.service import ALREADY_DECIDED, AssistantService
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
    ActionKind,
    Alert,
    AlertStatus,
    AssistantMode,
    AssistantRequest,
    AssistantStatus,
    NotificationChannel,
    PendingActionStatus,
    Severity,
)
from tests.factories import MISSION_ID, TMS_NOW
from tests.fake_llm import FakeLlm, reply, tool_call
from tests.fake_tms import FakeTms

pytestmark = pytest.mark.integration

OPERATOR = "ops_awa"
COLLEAGUE = "ops_karim"
REASON = "Détour autorisé par le client, confirmé par téléphone."


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


@pytest.fixture
def channel() -> RecordingCallChannel:
    return RecordingCallChannel()


def build(database, mcp, llm: FakeLlm, *, channel=None) -> AssistantService:
    calls = DriverCallService(channel, guardrails=CallGuardrails()) if channel else None
    return AssistantService(
        llm,  # type: ignore[arg-type]
        mcp,
        AssistantActions(database, mcp, driver_calls=calls),
        database=database,
    )


def ask(message: str, **overrides) -> AssistantRequest:
    base = {
        "operator_id": OPERATOR,
        "message": message,
        "mode": AssistantMode.CHAT,
        "tms_ids": ("x_tms",),
        "allow_actions": True,
    }
    return AssistantRequest(**{**base, **overrides})


async def seed_alert(database, *, mission_id: str = MISSION_ID, title: str = "Dérive d'itinéraire"):
    alert = Alert(
        tms_id="x_tms",
        mission_id=mission_id,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=Severity.HIGH,
        category="route_deviation",
        title=title,
        message="Le véhicule s'est écarté.",
        deduplication_key=f"deviation:x_tms:{mission_id}",
        detected_at=TMS_NOW,
    )
    async with database.transaction() as connection:
        alert_id = await AlertRepository().insert_alert(connection, alert)
    assert alert_id is not None
    return alert_id


async def propose_closure(database, mcp, alert_id, *, operator_id: str = OPERATOR):
    """Fait proposer une clôture par le modèle, et rend l'action en attente."""
    llm = FakeLlm(
        [
            reply(calls=[tool_call("close_alert", {"alert_id": str(alert_id), "reason": REASON})]),
            reply(content="Je propose de clore cette alerte. Validez-vous ?"),
        ]
    )
    answer = await build(database, mcp, llm).respond(ask("Clos cette alerte.", operator_id=operator_id))
    assert answer.status is AssistantStatus.AWAITING_CONFIRMATION
    return answer.pending_actions[0]


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


class TestSubstitutionImpossible:
    async def test_les_arguments_valides_sont_ceux_executes(self, database, mcp):
        """Le modèle ne repasse pas : il ne peut pas changer de cible.

        Deux alertes existent. Le modèle en propose une ; au moment de la
        validation, un modèle tout autre — qui ne nommerait que la seconde —
        ne change rien : ce sont les arguments figés qui s'exécutent.
        """
        premiere = await seed_alert(database, mission_id="msn_1", title="Dérive d'itinéraire")
        seconde = await seed_alert(database, mission_id="msn_2", title="Retard de livraison")
        pending = await propose_closure(database, mcp, premiere)

        # Un modèle qui, à la reprise, voudrait clore la seconde.
        hostile = FakeLlm(
            [
                reply(calls=[tool_call("close_alert", {"alert_id": str(seconde), "reason": REASON})]),
                reply(content="Fait."),
            ]
        )
        await build(database, mcp, hostile).decide(
            pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        async with database.connection() as connection:
            statuts = {
                row["id"]: row["status"]
                for row in (await connection.execute(select(alerts.c.id, alerts.c.status))).mappings()
            }
        assert statuts[premiere] == AlertStatus.RESOLVED.value
        # La seconde n'est pas close *sans validation* : la proposition que le
        # modèle a tentée à la reprise attend, elle aussi, un accord.
        assert statuts[seconde] == AlertStatus.OPEN.value

    async def test_description_construite_par_le_code(self, database, mcp):
        """L'opérateur lit ce qui s'exécutera, pas la prose du modèle."""
        alert_id = await seed_alert(database, title="Immobilisation prolongée")

        pending = await propose_closure(database, mcp, alert_id)

        assert "Immobilisation prolongée" in pending.summary
        assert "deviation_persistent" in pending.summary
        assert pending.arguments["alert_id"] == str(alert_id)
        assert pending.reason == REASON


class TestUsageUnique:
    async def test_deux_validations_simultanees_une_execution(self, database, mcp, channel):
        """Deux onglets, un seul appel au chauffeur."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "call_driver",
                            {
                                "tms_id": "x_tms",
                                "mission_id": MISSION_ID,
                                "reason": "Demander la raison de l'arrêt.",
                            },
                        )
                    ]
                ),
                reply(content="Je propose d'appeler le chauffeur."),
            ]
        )
        service = build(database, mcp, llm, channel=channel)
        pending = (await service.respond(ask("Appelle-le."))).pending_actions[0]

        premier, second = await asyncio.gather(
            build(database, mcp, FakeLlm([reply(content="Appel passé.")]), channel=channel).decide(
                pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
            ),
            build(database, mcp, FakeLlm([reply(content="Appel passé.")]), channel=channel).decide(
                pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
            ),
        )

        # Un seul téléphone a sonné, et l'autre requête le dit franchement.
        assert len(channel.sent) == 1
        assert {premier.answer, second.answer} & {ALREADY_DECIDED}
        assert await count(database, driver_calls) == 1

    async def test_validation_apres_refus_refusee(self, database, mcp):
        alert_id = await seed_alert(database)
        pending = await propose_closure(database, mcp, alert_id)
        service = build(database, mcp, FakeLlm([]))

        await service.decide(pending.id, operator_id=OPERATOR, decision=ActionDecision.REJECT)
        apres = await service.decide(pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM)

        assert apres.answer == ALREADY_DECIDED
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value


class TestPeremption:
    async def test_accord_perime_nexecute_rien(self, database, mcp):
        """Un accord donné il y a deux heures ne dit rien de maintenant."""
        alert_id = await seed_alert(database)
        repository = PendingActionRepository()
        conversation = ConversationRepository()
        async with database.transaction() as connection:
            conversation_id = await conversation.open(
                connection, operator_id=OPERATOR, title="t", mode=AssistantMode.CHAT
            )
            pending = await repository.propose(
                connection,
                conversation_id=conversation_id,
                operator_id=OPERATOR,
                kind=ActionKind.CLOSE_ALERT,
                arguments={"alert_id": str(alert_id), "reason": REASON},
                summary="Clore l'alerte",
                reason=REASON,
                alert_id=alert_id,
                now=datetime.now(UTC) - DEFAULT_TTL - timedelta(minutes=1),
            )

        answer = await build(database, mcp, FakeLlm([])).decide(
            pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert answer.answer == ALREADY_DECIDED
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value

    async def test_perimee_absente_de_la_liste(self, database, mcp):
        """Entre deux balayages, l'interface ne doit pas la proposer."""
        alert_id = await seed_alert(database)
        repository = PendingActionRepository()
        async with database.transaction() as connection:
            conversation_id = await ConversationRepository().open(
                connection, operator_id=OPERATOR, title="t", mode=AssistantMode.CHAT
            )
            await repository.propose(
                connection,
                conversation_id=conversation_id,
                operator_id=OPERATOR,
                kind=ActionKind.CLOSE_ALERT,
                arguments={"alert_id": str(alert_id), "reason": REASON},
                summary="Clore l'alerte",
                reason=REASON,
                now=datetime.now(UTC) - DEFAULT_TTL - timedelta(minutes=1),
            )

        attente = await build(database, mcp, FakeLlm([])).waiting_actions(OPERATOR)

        assert attente == []

    async def test_balayage_solde_les_perimees(self, database, mcp):
        alert_id = await seed_alert(database)
        async with database.transaction() as connection:
            conversation_id = await ConversationRepository().open(
                connection, operator_id=OPERATOR, title="t", mode=AssistantMode.CHAT
            )
            await PendingActionRepository().propose(
                connection,
                conversation_id=conversation_id,
                operator_id=OPERATOR,
                kind=ActionKind.CLOSE_ALERT,
                arguments={"alert_id": str(alert_id), "reason": REASON},
                summary="Clore l'alerte",
                reason=REASON,
                now=datetime.now(UTC) - DEFAULT_TTL - timedelta(minutes=1),
            )

        counts = await build(database, mcp, FakeLlm([])).sweep_actions()

        assert counts["expired"] == 1
        async with database.connection() as connection:
            status = (await connection.execute(select(assistant_pending_actions.c.status))).scalar_one()
        assert status == PendingActionStatus.EXPIRED.value

    async def test_balayage_solde_une_execution_perdue(self, database, mcp):
        """Un processus tué entre la validation et l'exécution.

        Sans ce solde, l'interface afficherait « en cours » sur un geste que
        plus personne ne tient — et l'opérateur attendrait un résultat qui ne
        viendra jamais.
        """
        alert_id = await seed_alert(database)
        repository = PendingActionRepository()
        async with database.transaction() as connection:
            conversation_id = await ConversationRepository().open(
                connection, operator_id=OPERATOR, title="t", mode=AssistantMode.CHAT
            )
            pending = await repository.propose(
                connection,
                conversation_id=conversation_id,
                operator_id=OPERATOR,
                kind=ActionKind.CLOSE_ALERT,
                arguments={"alert_id": str(alert_id), "reason": REASON},
                summary="Clore l'alerte",
                reason=REASON,
            )
        # Prise il y a longtemps, jamais soldée : le processus est mort ici.
        async with database.transaction() as connection:
            await repository.claim(
                connection,
                pending.id,
                operator_id=OPERATOR,
                now=datetime.now(UTC) - timedelta(hours=1),
            )

        counts = await build(database, mcp, FakeLlm([])).sweep_actions()

        assert counts["abandoned"] == 1
        async with database.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(
                            assistant_pending_actions.c.status,
                            assistant_pending_actions.c.failure_reason,
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == PendingActionStatus.FAILED.value
        # Le motif est écrit : « n'a pas été effectuée » est ce que
        # l'opérateur doit lire, et non un silence.
        assert "n'a pas été effectuée" in row["failure_reason"]


class TestMandat:
    async def test_un_collegue_ne_tranche_pas(self, database, mcp):
        """Même mieux habilité, il n'a pas le mandat de cette proposition."""
        alert_id = await seed_alert(database)
        pending = await propose_closure(database, mcp, alert_id)

        answer = await build(database, mcp, FakeLlm([])).decide(
            pending.id, operator_id=COLLEAGUE, decision=ActionDecision.CONFIRM
        )

        assert answer.answer == ALREADY_DECIDED
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.OPEN.value

    async def test_liste_restreinte_a_son_destinataire(self, database, mcp):
        alert_id = await seed_alert(database)
        await propose_closure(database, mcp, alert_id)
        service = build(database, mcp, FakeLlm([]))

        assert len(await service.waiting_actions(OPERATOR)) == 1
        assert await service.waiting_actions(COLLEAGUE) == []

    async def test_action_inconnue(self, database, mcp):
        answer = await build(database, mcp, FakeLlm([])).decide(
            uuid.uuid4(), operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert answer.answer == ALREADY_DECIDED


class TestReprise:
    async def test_le_modele_rend_compte_du_resultat_reel(self, database, mcp):
        """La reprise lui donne le résultat, pas sa propre supposition."""
        alert_id = await seed_alert(database)
        pending = await propose_closure(database, mcp, alert_id)
        llm = FakeLlm([reply(content="C'est fait : l'alerte est close.")])

        answer = await build(database, mcp, llm).decide(
            pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert answer.answer == "C'est fait : l'alerte est close."
        contenu = " ".join(str(message["content"]) for message in llm.calls[0]["messages"])
        assert "closed" in contenu or "close" in contenu

    async def test_plan_en_deux_gestes(self, database, mcp, channel):
        """« Appelle le chauffeur, puis clos selon sa réponse. »

        Aucun état ne vit en mémoire entre les deux validations : la
        conversation *est* l'état, et chaque geste est validé séparément.
        """
        alert_id = await seed_alert(database)
        premier = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "call_driver",
                            {
                                "tms_id": "x_tms",
                                "mission_id": MISSION_ID,
                                "reason": "Demander la raison de l'écart constaté.",
                            },
                        )
                    ]
                ),
                reply(content="Je propose d'appeler le chauffeur d'abord."),
            ]
        )
        service = build(database, mcp, premier, channel=channel)
        appel = (
            await service.respond(ask("Appelle le chauffeur, puis clos l'alerte si le détour est justifié."))
        ).pending_actions[0]

        # Validation du premier geste : à la reprise, le modèle propose le second.
        suite = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "close_alert",
                            {
                                "alert_id": str(alert_id),
                                "reason": "Le chauffeur confirme un détour autorisé.",
                            },
                        )
                    ]
                ),
                reply(content="Appel passé. Je propose maintenant de clore l'alerte."),
            ]
        )
        apres_appel = await build(database, mcp, suite, channel=channel).decide(
            appel.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert len(channel.sent) == 1
        assert apres_appel.status is AssistantStatus.AWAITING_CONFIRMATION
        cloture = apres_appel.pending_actions[0]
        assert cloture.kind is ActionKind.CLOSE_ALERT

        # Validation du second geste.
        final = await build(database, mcp, FakeLlm([reply(content="Alerte close.")])).decide(
            cloture.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM
        )

        assert final.actions[0].kind is ActionKind.CLOSE_ALERT
        async with database.connection() as connection:
            status = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
        assert status == AlertStatus.RESOLVED.value

    async def test_validation_tracee_dans_la_conversation(self, database, mcp):
        """F25 : l'historique doit porter la décision humaine, pas seulement l'action."""
        alert_id = await seed_alert(database)
        pending = await propose_closure(database, mcp, alert_id)
        service = build(database, mcp, FakeLlm([reply(content="Fait.")]))

        await service.decide(pending.id, operator_id=OPERATOR, decision=ActionDecision.CONFIRM)
        messages = await service.history(pending.conversation_id, OPERATOR)

        assert any("[action validée]" in message.content for message in messages)

    async def test_refus_trace_dans_la_conversation(self, database, mcp):
        alert_id = await seed_alert(database)
        pending = await propose_closure(database, mcp, alert_id)
        service = build(database, mcp, FakeLlm([]))

        await service.decide(pending.id, operator_id=OPERATOR, decision=ActionDecision.REJECT)
        messages = await service.history(pending.conversation_id, OPERATOR)

        assert any("[action refusée]" in message.content for message in messages)


class TestPartageDesGestes:
    def test_seuls_les_gestes_qui_sortent_du_systeme_se_valident(self):
        """Consigner une décision n'écrit qu'une ligne de journal dictée."""
        assert ActionKind.CLOSE_ALERT in REQUIRES_CONFIRMATION
        assert ActionKind.CALL_DRIVER in REQUIRES_CONFIRMATION
        assert ActionKind.LOG_DECISION not in REQUIRES_CONFIRMATION

    async def test_decision_consignee_sans_validation(self, database, mcp):
        """Une friction inutile finit par être cliquée sans lire."""
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call(
                            "log_decision",
                            {"tms_id": "x_tms", "reason": "Détour autorisé, aucune action."},
                        )
                    ]
                ),
                reply(content="C'est consigné."),
            ]
        )

        answer = await build(database, mcp, llm).respond(ask("Note le détour."))

        assert answer.status is AssistantStatus.ANSWERED
        assert answer.actions[0].kind is ActionKind.LOG_DECISION
        assert await count(database, assistant_pending_actions) == 0
