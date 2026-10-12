"""Tests du rapport de situation à heure fixe (F24).

Ce qui se joue ici n'est pas la qualité du rapport — c'est le mode `report` de
l'assistant, déjà éprouvé — mais son **déclenchement** :

  - un créneau n'est produit **qu'une fois**, même avec plusieurs instances du
    service agent ;
  - un créneau manqué n'est pas rattrapé indéfiniment : un service arrêté
    toute la nuit ne doit pas produire au démarrage les quatre rapports qu'il
    a ratés ;
  - un rapport **ne notifie personne** (F26). Il est conservé et lu.

Les heures sont celles de l'exploitation, pas UTC : « 8 h » doit rester 8 h
pour l'agent de suivi.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from database.schema import alerts, conversations, notification_outbox
from services.agents.assistant.actions import AssistantActions
from services.agents.assistant.repository import ConversationRepository
from services.agents.assistant.schedule import ReportSchedule, ReportScheduler
from services.agents.assistant.service import AssistantService
from services.agents.assistant.tools import ASSISTANT_READ_TOOLS
from services.agents.integrations.mcp_client import AgentMcpClient
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from shared.mapped_dto import AssistantMode
from tests.fake_llm import FakeLlm, reply, unavailable
from tests.fake_tms import FakeTms

pytestmark = pytest.mark.integration

PARIS = ZoneInfo("Europe/Paris")
OPERATOR = "scheduled_report"


@pytest.fixture
async def mcp():
    tms = FakeTms()
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with AgentMcpClient(server, allowed=ASSISTANT_READ_TOOLS) as client:
        yield client
    await context.aclose()


def build(database, mcp, llm: FakeLlm, *, times=("08:00", "14:00")) -> ReportScheduler:
    assistant = AssistantService(
        llm,  # type: ignore[arg-type]
        mcp,
        AssistantActions(database, mcp),
        database=database,
    )
    schedule = ReportSchedule.parse(times, timezone="Europe/Paris", catch_up_minutes=30)
    return ReportScheduler(assistant, database, schedule, operator_id=OPERATOR)


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


class TestCalendrier:
    def test_creneau_du_moment(self):
        schedule = ReportSchedule.parse(("08:00",), timezone="Europe/Paris")

        # 8 h 05 à Paris : le créneau de 8 h est dû.
        assert schedule.due_slot(datetime(2026, 10, 11, 8, 5, tzinfo=PARIS)) == "2026-10-11T08:00"

    def test_heures_exprimees_dans_le_fuseau_de_lexploitation(self):
        """« 8 h » veut dire 8 h pour l'agent de suivi, pas 8 h UTC."""
        schedule = ReportSchedule.parse(("08:00",), timezone="Europe/Paris")

        # 6 h 05 UTC = 8 h 05 à Paris en heure d'été.
        assert schedule.due_slot(datetime(2026, 10, 11, 6, 5, tzinfo=UTC)) == "2026-10-11T08:00"
        # 8 h 05 UTC = 10 h 05 à Paris : plus rien à produire.
        assert schedule.due_slot(datetime(2026, 10, 11, 8, 5, tzinfo=UTC)) is None

    def test_creneau_trop_ancien_abandonne(self):
        """Un rapport de la veille n'intéresse personne."""
        schedule = ReportSchedule.parse(("08:00",), timezone="Europe/Paris", catch_up_minutes=30)

        assert schedule.due_slot(datetime(2026, 10, 11, 9, 0, tzinfo=PARIS)) is None

    def test_seul_le_creneau_le_plus_recent(self):
        """Pas de rafale au démarrage d'un service arrêté depuis la veille."""
        schedule = ReportSchedule.parse(
            ("08:00", "14:00", "18:00"), timezone="Europe/Paris", catch_up_minutes=600
        )

        assert schedule.due_slot(datetime(2026, 10, 11, 18, 10, tzinfo=PARIS)) == "2026-10-11T18:00"

    def test_creneau_de_la_veille_encore_frais(self):
        """Juste après minuit, le créneau du soir compte encore si configuré ainsi."""
        schedule = ReportSchedule.parse(("23:50",), timezone="Europe/Paris", catch_up_minutes=30)

        assert schedule.due_slot(datetime(2026, 10, 12, 0, 10, tzinfo=PARIS)) == "2026-10-11T23:50"

    def test_sans_heure_aucun_rapport(self):
        assert ReportSchedule.parse(()).due_slot(datetime.now(UTC)) is None


class TestProduction:
    async def test_rapport_produit_et_conserve(self, database, mcp):
        llm = FakeLlm([reply(content="1 mission en cours, aucune alerte ouverte.")])
        scheduler = build(database, mcp, llm)

        slot = await scheduler.run_once(datetime(2026, 10, 11, 8, 2, tzinfo=PARIS))

        assert slot == "2026-10-11T08:00"
        async with database.connection() as connection:
            rows = await ConversationRepository().recent(connection, OPERATOR, mode=AssistantMode.REPORT)
            texte = await ConversationRepository().last_answer(connection, rows[0]["id"])  # type: ignore[arg-type]
        assert len(rows) == 1
        assert texte == "1 mission en cours, aucune alerte ouverte."

    async def test_hors_creneau_rien_nest_produit(self, database, mcp):
        scheduler = build(database, mcp, FakeLlm([]))

        slot = await scheduler.run_once(datetime(2026, 10, 11, 11, 0, tzinfo=PARIS))

        assert slot is None
        assert await count(database, conversations) == 0

    async def test_un_creneau_une_seule_fois(self, database, mcp):
        """Le cas d'une instance redémarrée à 8 h 02."""
        moment = datetime(2026, 10, 11, 8, 2, tzinfo=PARIS)
        premier = await build(database, mcp, FakeLlm([reply(content="rapport")])).run_once(moment)

        # Nouvelle instance, nouveau processus : rien en mémoire.
        second = await build(database, mcp, FakeLlm([reply(content="doublon")])).run_once(
            datetime(2026, 10, 11, 8, 20, tzinfo=PARIS)
        )

        assert premier == "2026-10-11T08:00"
        assert second is None
        assert await count(database, conversations) == 1

    async def test_creneau_suivant_produit(self, database, mcp):
        await build(database, mcp, FakeLlm([reply(content="matin")])).run_once(
            datetime(2026, 10, 11, 8, 2, tzinfo=PARIS)
        )

        apres_midi = await build(database, mcp, FakeLlm([reply(content="après-midi")])).run_once(
            datetime(2026, 10, 11, 14, 1, tzinfo=PARIS)
        )

        assert apres_midi == "2026-10-11T14:00"
        assert await count(database, conversations) == 2

    async def test_modele_indisponible_creneau_consomme(self, database, mcp):
        """Sinon la production boucle toutes les minutes jusqu'à la fin du créneau."""
        slot = await build(database, mcp, FakeLlm([unavailable()])).run_once(
            datetime(2026, 10, 11, 8, 2, tzinfo=PARIS)
        )
        retente = await build(database, mcp, FakeLlm([reply(content="rapport")])).run_once(
            datetime(2026, 10, 11, 8, 10, tzinfo=PARIS)
        )

        assert slot == "2026-10-11T08:00"
        assert retente is None
        # La conversation existe et dit que l'assistant était absent : c'est
        # une information, et l'interface doit pouvoir l'afficher.
        async with database.connection() as connection:
            rows = await ConversationRepository().recent(connection, OPERATOR)
            texte = await ConversationRepository().last_answer(connection, rows[0]["id"])  # type: ignore[arg-type]
        assert "indisponible" in str(texte)


class TestCeQuilNeFaitPas:
    async def test_ne_notifie_personne(self, database, mcp):
        """F26 : un rapport poussé passerait pour un substitut aux alertes."""
        await build(database, mcp, FakeLlm([reply(content="rapport")])).run_once(
            datetime(2026, 10, 11, 8, 2, tzinfo=PARIS)
        )

        assert await count(database, notification_outbox) == 0
        assert await count(database, alerts) == 0

    async def test_ne_peut_pas_agir(self, database, mcp):
        """Personne n'est là à 8 h pour accorder le droit d'agir."""
        llm = FakeLlm([reply(content="rapport")])
        await build(database, mcp, llm).run_once(datetime(2026, 10, 11, 8, 2, tzinfo=PARIS))

        proposes = llm.calls[0]["tools"]
        assert "close_alert" not in proposes
        assert "call_driver" not in proposes
