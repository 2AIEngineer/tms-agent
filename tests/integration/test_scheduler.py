"""Tests de la boucle de surveillance.

PostgreSQL réel, serveur MCP en mémoire sur le double du TMS. Ce qui est
vérifié ici, c'est l'orchestration : ce qu'un cycle fait, ce qu'il signale, et
surtout ce qui ne l'arrête pas.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from database.schema import alerts, monitoring_state
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from services.monitoring.alerts.service import AlertService
from services.monitoring.integrations.mcp_client import McpClient
from services.monitoring.rules.loader import RuleRepository
from services.monitoring.scheduler import MonitoringCycle
from services.monitoring.state import MonitoringStateRepository, cycle_key
from shared.mapped_dto import MissionStatus
from shared.utils.db import try_advisory_lock
from tests.conftest import RULES_DIRECTORY
from tests.fake_tms import TMS_NOW, FakeTms, event_payload, iso, mission_payload

pytestmark = pytest.mark.integration


@pytest.fixture
def tms() -> FakeTms:
    """TMS portant une mission en dérive persistante : 7,2 km depuis 25 minutes."""
    return FakeTms()


@pytest.fixture
async def mcp(tms: FakeTms):
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with McpClient(server) as client:
        yield client
    await context.aclose()


@pytest.fixture
def cycle(database, mcp, rules_dir=None):
    return MonitoringCycle(
        database,
        mcp,
        RuleRepository(RULES_DIRECTORY),
        AlertService(database),
    )


async def rows(database, table):
    async with database.connection() as connection:
        return list((await connection.execute(select(table))).mappings())


class TestCycleNominal:
    async def test_detection_de_bout_en_bout(self, cycle, database):
        """TMS -> pivot -> règles -> alerte -> notifications en file, en un cycle."""
        report = await cycle.run_once("x_tms")

        assert report.failed is False
        # Une heure par TMS récolté, et non une heure globale : deux TMS n'ont
        # aucune raison d'être à la même heure.
        assert report.tms_time == {"x_tms": TMS_NOW}
        assert report.missions == 1
        assert report.missions_by_tms == {"x_tms": 1}
        assert report.alerts_created == 2  # dérive persistante + retard ETA
        assert report.active_rules == 6
        assert report.rule_errors == []

        stored = {row["rule_id"]: row for row in await rows(database, alerts)}
        assert set(stored) == {"deviation_persistent", "eta_delay"}
        assert stored["deviation_persistent"]["detected_at"] == TMS_NOW

    async def test_deuxieme_cycle_ne_redetecte_pas(self, cycle, database):
        """Le cycle tourne toutes les 10 s : la dérive ne doit alerter qu'une fois."""
        await cycle.run_once("x_tms")
        second = await cycle.run_once("x_tms")

        assert second.alerts_created == 0
        assert second.alerts_suppressed == 2
        assert len(await rows(database, alerts)) == 2

    async def test_missions_terminees_ignorees(self, cycle, tms, database):
        tms.missions = [
            mission_payload("msn_1", status="completed"),
            mission_payload("msn_2", status="cancelled"),
        ]
        report = await cycle.run_once("x_tms")

        assert report.missions == 0
        assert report.alerts_created == 0

    async def test_alertes_a_enqueter_nommees_par_gravite(self, cycle):
        """F19 : le bilan nomme ce qui doit être analysé, le plus grave d'abord."""
        report = await cycle.run_once("x_tms")
        assert len(report.investigable) == 2

    async def test_plusieurs_missions_en_un_cycle(self, cycle, tms, database):
        tms.missions = [mission_payload(f"msn_{index}") for index in range(5)]
        report = await cycle.run_once("x_tms")

        assert report.missions == 5
        assert report.alerts_created == 10  # deux règles par mission
        assert len(await rows(database, alerts)) == 10


class TestEvenements:
    async def test_evenement_critique_traite_une_fois(self, cycle, tms, database):
        """F7 : le TMS renvoie toute la chronologie à chaque appel."""
        tms.missions[0]["events"] = [
            event_payload(
                "evt_crit",
                "driver_message",
                TMS_NOW,
                payload={"severity": "critical", "text": "Accident sur A6"},
            )
        ]

        premier = await cycle.run_once("x_tms")
        assert premier.events_processed == 1
        assert any(row["rule_id"] == "critical_incident" for row in await rows(database, alerts))

        second = await cycle.run_once("x_tms")
        assert second.events_processed == 0  # déjà traité

    async def test_evenements_anodins_marques_traites(self, cycle, tms):
        """Sinon ils seraient réévalués à chaque cycle, indéfiniment."""
        premier = await cycle.run_once("x_tms")
        assert premier.events_processed == 3  # les trois événements de la mission
        assert (await cycle.run_once("x_tms")).events_processed == 0


class TestResilience:
    async def test_mission_illisible_signalee_sans_arreter_le_cycle(self, cycle, tms, database):
        """F4 : les missions lisibles sont traitées, les autres sont nommées."""
        tms.missions = [mission_payload("msn_1"), mission_payload("msn_2")]
        tms.fail("/missions/msn_2", 503)

        report = await cycle.run_once("x_tms")

        assert report.missions == 1
        # Indexé par provenance : deux TMS peuvent porter une mission `msn_2`.
        assert "x_tms:msn_2" in report.unreadable_missions
        assert report.unreadable_missions["x_tms:msn_2"].retryable is True
        assert report.alerts_created == 2  # msn_1 a bien été surveillée

    async def test_instantane_partiel_compte_et_exploite(self, cycle, tms):
        """Un écart illisible n'empêche pas de détecter le retard."""
        tms.fail("/missions/msn_1/deviation", 503)

        report = await cycle.run_once("x_tms")

        assert report.partial_snapshots == 1
        assert report.alerts_created == 1  # le retard ETA seul

    async def test_tms_injoignable_met_le_cycle_en_echec(self, cycle, tms):
        """F5 : rien à lire, donc rien à décider — l'appelant appliquera un backoff."""
        tms.fail("/health", 503)

        report = await cycle.run_once("x_tms")

        assert report.failed is True
        assert report.failure_reason
        assert report.alerts_created == 0

    async def test_regle_invalide_signalee_sans_desarmer_la_surveillance(self, database, mcp, tmp_path):
        """F8 : une faute de frappe ne doit pas arrêter la détection."""
        rules_dir = tmp_path / "definitions"
        rules_dir.mkdir()
        for source in RULES_DIRECTORY.glob("*.yaml"):
            (rules_dir / source.name).write_text(source.read_text(), encoding="utf-8")
        (rules_dir / "cassee.yaml").write_text("id: cassee\nwhen: [: ::", encoding="utf-8")

        cycle = MonitoringCycle(database, mcp, RuleRepository(rules_dir), AlertService(database))
        report = await cycle.run_once("x_tms")

        assert report.active_rules == 6  # les règles valides restent actives
        assert len(report.rule_errors) == 1
        assert report.alerts_created == 2


class TestBattementDeCoeur:
    async def test_cycle_reussi_enregistre(self, cycle, database):
        """La panne la plus grave est silencieuse : ce battement la rend visible."""
        await cycle.run_once("x_tms")

        state = await rows(database, monitoring_state)
        assert len(state) == 1
        assert state[0]["key"] == "last_cycle:x_tms"
        assert state[0]["value"]["failed"] is False
        assert state[0]["value"]["missions"] == 1
        assert state[0]["value"]["missions_by_tms"] == {"x_tms": 1}
        assert state[0]["value"]["alerts_created"] == 2

    async def test_cycle_en_echec_enregistre_aussi(self, cycle, database, tms):
        """« Le worker tourne mais ne lit rien » et « le worker est mort » sont
        deux pannes distinctes, qui n'appellent pas la même intervention."""
        tms.fail("/health", 503)
        await cycle.run_once("x_tms")

        state = await rows(database, monitoring_state)
        assert state[0]["value"]["failed"] is True
        assert state[0]["value"]["failure_reason"]

    async def test_battement_ecrase_le_precedent(self, cycle, database):
        await cycle.run_once("x_tms")
        await cycle.run_once("x_tms")
        assert len(await rows(database, monitoring_state)) == 1

    async def test_detection_de_lobsolescence(self, cycle, database):
        await cycle.run_once("x_tms")

        async with database.connection() as connection:
            heartbeat = await MonitoringStateRepository().last_cycle(connection, key=cycle_key("x_tms"))

        assert heartbeat is not None
        assert heartbeat.is_stale(poll_interval_s=10) is False
        # Trois intervalles plus tard, sans nouveau cycle, l'alarme doit sonner.
        assert (
            heartbeat.is_stale(poll_interval_s=10, now=heartbeat.updated_at + timedelta(seconds=31)) is True
        )

    async def test_aucun_battement_avant_le_premier_cycle(self, database):
        async with database.connection() as connection:
            heartbeat = await MonitoringStateRepository().last_cycle(connection, key=cycle_key("x_tms"))
        assert heartbeat is None


class TestInstanceUnique:
    async def test_le_verrou_consultatif_exclut_une_seconde_instance(self, database):
        """Deux instances évalueraient les mêmes missions et se disputeraient la file."""
        key = 0x746D7399

        async with database.engine.connect() as premiere:
            assert await try_advisory_lock(premiere, key) is True
            async with database.engine.connect() as seconde:
                assert await try_advisory_lock(seconde, key) is False

    async def test_le_verrou_se_libere_a_la_fermeture(self, database):
        """Porté par la connexion : un processus mort ne laisse pas de verrou orphelin."""
        key = 0x746D739A

        async with database.engine.connect() as connection:
            assert await try_advisory_lock(connection, key) is True

        async with database.engine.connect() as connection:
            assert await try_advisory_lock(connection, key) is True


class TestHeureTms:
    async def test_toutes_les_missions_jugees_sur_le_meme_instant(self, cycle, tms, database):
        """F3 : relire l'heure par mission les jugerait sur des instants différents."""
        tms.missions = [mission_payload(f"msn_{index}") for index in range(4)]

        await cycle.run_once("x_tms")

        detections = {row["detected_at"] for row in await rows(database, alerts)}
        assert detections == {TMS_NOW}

    async def test_horloge_tms_accelere(self, cycle, tms, database):
        """Le simulateur avance vite : les règles doivent le suivre, pas l'horloge locale."""
        await cycle.run_once("x_tms")

        plus_tard = TMS_NOW + timedelta(hours=4)
        tms.now = plus_tard
        tms.missions = [mission_payload("msn_1", status="completed")]
        for key in ("planned_delivery_at", "actual_delivery_at"):
            tms.missions[0][key] = iso(plus_tard)

        report = await cycle.run_once("x_tms")

        # La mission n'est plus surveillée : ses alertes sont closes (F13).
        assert report.missions == 0
        assert {row["status"] for row in await rows(database, alerts)} == {"open"}


class TestConcurrenceDesCycles:
    async def test_deux_cycles_simultanes_ne_doublent_pas_les_alertes(self, cycle, database):
        """Ne doit pas arriver — le verrou l'empêche — mais la base doit tenir.

        Le filet de sécurité est l'index unique partiel : même si deux cycles
        se chevauchaient lors d'un déploiement, aucune alerte ne serait doublée.
        """
        await asyncio.gather(cycle.run_once("x_tms"), cycle.run_once("x_tms"))

        assert len(await rows(database, alerts)) == 2


class TestStatuts:
    async def test_missions_planifiees_surveillees(self, cycle, tms):
        """Une mission qui devait démarrer et ne démarre pas est une anomalie."""
        tms.missions = [mission_payload("msn_1", status="planned")]
        report = await cycle.run_once("x_tms")
        assert report.missions == 1

    async def test_statut_inconnu_signale_quand_le_tms_ignore_le_filtre(self, cycle, tms):
        """Certains TMS renvoient tout quand ils ne gèrent pas un filtre.

        Dans ce cas, une mission au statut inconnu arrive jusqu'à nous : elle
        doit être écartée et **nommée** dans le bilan. La taire reviendrait à
        croire la flotte surveillée alors qu'une mission échappe au suivi.
        """
        tms.ignore_status_filter = True
        mission = mission_payload("msn_9")
        mission["status"] = "en_attente_de_quai"
        tms.missions = [mission]

        report = await cycle.run_once("x_tms")

        assert report.missions == 0
        assert "x_tms:msn_9" in report.unreadable_missions

    async def test_missions_hors_perimetre_non_surveillees(self, cycle, tms):
        """Un TMS qui ignore le filtre ne doit pas nous faire alerter sur du terminé."""
        tms.ignore_status_filter = True
        tms.missions = [
            mission_payload("msn_1", status="in_progress"),
            mission_payload("msn_2", status="completed"),
        ]

        report = await cycle.run_once("x_tms")

        assert report.missions == 1
        assert report.alerts_created == 2

    async def test_statuts_surveilles(self):
        from services.mcp_server.snapshots import WATCHED_STATUSES

        assert set(WATCHED_STATUSES) == {
            MissionStatus.PLANNED.value,
            MissionStatus.IN_PROGRESS.value,
        }
