"""Tests du raccordement simultané de plusieurs TMS.

Deux défauts de correction étaient latents tant que `mission_id` et `event_id`
étaient traités comme des clés globales. Les deux étaient **silencieux** — et
c'est ce qui les rendait dangereux : l'absence d'alerte ressemble en tout point
à une flotte sans anomalie.

  1. l'index unique partiel des alertes ouvertes portait sur la seule clé de
     déduplication : l'alerte du second TMS était supprimée comme doublon du
     premier, et un camion en dérive ne déclenchait rien ;
  2. la clé primaire de `processed_events` était l'identifiant d'événement
     seul : un événement du second TMS passait pour déjà traité, et son alerte
     ne partait jamais.

Les deux premiers tests de ce module échouaient avant la dimension `tms_id`.
"""

import asyncio

import pytest
from sqlalchemy import func, select

from database.schema import alerts, notification_outbox, notifications, processed_events
from services.mcp_server.connections import TmsConnection, UnknownTms
from services.mcp_server.connectors.dialect import load_named_dialect
from services.mcp_server.connectors.http_tms import HttpTmsConnector
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from services.monitoring.alerts.service import AlertService
from services.monitoring.integrations.mcp_client import McpClient
from services.monitoring.notifications import NotificationService
from services.monitoring.rules.engine import evaluate_event_rules, evaluate_state_rules
from services.monitoring.rules.loader import RuleRepository
from services.monitoring.scheduler import MonitoringCycle
from shared.channels import ConsoleChannel
from shared.mapped_dto import EventType, NotificationChannel
from tests.conftest import RULES_DIRECTORY
from tests.factories import make_event, make_snapshot
from tests.fake_fleetlink import FakeFleetLink, transport_payload
from tests.fake_tms import TMS_NOW, FakeTms, mission_payload

pytestmark = pytest.mark.integration


# --- Deux TMS raccordés, dont un partage les identifiants de l'autre ---------


@pytest.fixture
def colliding_tms() -> FakeTms:
    """Second TMS du **même dialecte**, portant la même mission `msn_1`.

    Le cas réel : deux instances du même logiciel de TMS chez deux clients.
    Rien n'empêche leurs identifiants de coïncider — ils sont internes à chacun.
    """
    return FakeTms(missions=[mission_payload("msn_1", reference="AUTRE-CLIENT-0001")])


@pytest.fixture
async def two_tms_context(tms_pair):
    """Contexte MCP avec deux connexions distinctes sur le même dialecte."""
    first, second = tms_pair
    connections = [
        TmsConnection.from_parts(
            RestApiTmsConnector("http://a.test/v1", client=first.client(), retry_backoff_s=0.001),
            tms_id="client_a",
        ),
        TmsConnection.from_parts(
            RestApiTmsConnector("http://b.test/v1", client=second.client(), retry_backoff_s=0.001),
            tms_id="client_b",
        ),
    ]
    context = McpContext.for_connections(connections)
    yield context
    await context.aclose()


@pytest.fixture
def tms_pair(colliding_tms):
    return FakeTms(), colliding_tms


@pytest.fixture
async def two_tms_mcp(two_tms_context):
    server, _ = build_server(context=two_tms_context)
    async with McpClient(server) as client:
        yield client


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


async def rows(database, table):
    async with database.connection() as connection:
        return list((await connection.execute(select(table))).mappings())


class TestCollisionDesAlertes:
    """Le défaut n° 1 : l'alerte du second TMS supprimée comme doublon."""

    async def test_deux_tms_meme_mission_deux_alertes(self, database, shipped_rules):
        service = AlertService(database)
        for tms_id in ("client_a", "client_b"):
            snapshot = make_snapshot(
                tms_id=tms_id, mission_id="msn_1", deviation_km=7.2, deviation_minutes=25
            )
            report = await service.process_state_evaluations(
                snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
            )
            assert len(report.created) == 1, f"alerte perdue pour {tms_id}"

        stored = await rows(database, alerts)
        assert len(stored) == 2
        # Même clé de déduplication, provenances différentes : c'est la
        # provenance qui sépare les deux, pas le gabarit de la règle.
        assert {row["deduplication_key"] for row in stored} == {"deviation:msn_1"}
        assert {row["tms_id"] for row in stored} == {"client_a", "client_b"}

    async def test_deduplication_toujours_active_au_sein_dun_tms(self, database, shipped_rules):
        """La correction ne doit pas affaiblir la déduplication (F11)."""
        service = AlertService(database)
        snapshot = make_snapshot(
            tms_id="client_a", mission_id="msn_1", deviation_km=7.2, deviation_minutes=25
        )

        premier = await service.process_state_evaluations(
            snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
        )
        second = await service.process_state_evaluations(
            snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
        )

        assert len(premier.created) == 1
        assert second.created == []
        assert len(second.suppressed) == 1
        assert await count(database, alerts) == 1

    async def test_cooldown_cantonne_a_un_tms(self, database, shipped_rules):
        """Le cooldown d'un client ne doit pas étouffer l'alerte d'un autre."""
        from datetime import timedelta

        service = AlertService(database)

        # Client A : alerte, puis retour à la normale (alerte close).
        await service.process_state_evaluations(
            (
                a := make_snapshot(
                    tms_id="client_a", mission_id="msn_1", deviation_km=7.2, deviation_minutes=25
                )
            ),
            evaluate_state_rules(shipped_rules.rules, a),
        )
        retour = make_snapshot(
            tms_id="client_a",
            mission_id="msn_1",
            deviation_km=0.2,
            observed_at=TMS_NOW + timedelta(minutes=5),
        )
        await service.process_state_evaluations(retour, evaluate_state_rules(shipped_rules.rules, retour))

        # Client B dérive dans la fenêtre de cooldown de A : il doit alerter.
        b = make_snapshot(
            tms_id="client_b",
            mission_id="msn_1",
            deviation_km=7.2,
            deviation_minutes=25,
            observed_at=TMS_NOW + timedelta(minutes=10),
        )
        report = await service.process_state_evaluations(b, evaluate_state_rules(shipped_rules.rules, b))

        assert len(report.created) == 1

    async def test_cloture_automatique_ne_touche_pas_lautre_tms(self, database, shipped_rules):
        """Le camion de A rentre dans le rang ; l'alerte de B reste ouverte."""
        from datetime import timedelta

        service = AlertService(database)
        for tms_id in ("client_a", "client_b"):
            snapshot = make_snapshot(
                tms_id=tms_id, mission_id="msn_1", deviation_km=7.2, deviation_minutes=25
            )
            await service.process_state_evaluations(
                snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
            )

        retour = make_snapshot(
            tms_id="client_a",
            mission_id="msn_1",
            deviation_km=0.2,
            observed_at=TMS_NOW + timedelta(minutes=10),
        )
        report = await service.process_state_evaluations(
            retour, evaluate_state_rules(shipped_rules.rules, retour)
        )

        assert len(report.resolved) == 1
        statuts = {row["tms_id"]: row["status"] for row in await rows(database, alerts)}
        assert statuts == {"client_a": "resolved", "client_b": "open"}

    async def test_insertions_concurrentes_entre_deux_tms(self, database, shipped_rules):
        """Deux TMS écrivant en même temps sur la même clé : deux alertes.

        Le verrou consultatif porte sur la provenance **et** la clé : les deux
        écritures ne s'attendent pas, et aucune n'est perdue.
        """
        snapshots = [
            make_snapshot(tms_id=tms_id, mission_id="msn_1", deviation_km=7.2, deviation_minutes=25)
            for tms_id in ("client_a", "client_b")
        ]
        rapports = await asyncio.gather(
            *(
                AlertService(database).process_state_evaluations(
                    snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
                )
                for snapshot in snapshots
            )
        )

        assert sum(len(rapport.created) for rapport in rapports) == 2
        assert await count(database, alerts) == 2


class TestCollisionDesEvenements:
    """Le défaut n° 2 : l'événement du second TMS pris pour déjà traité."""

    async def test_meme_identifiant_devenement_dans_deux_tms(self, database, shipped_rules):
        service = AlertService(database)
        event = make_event(
            event_id="evt_crit",
            event_type=EventType.DRIVER_MESSAGE,
            payload={"severity": "critical", "text": "Accident"},
        )

        for tms_id in ("client_a", "client_b"):
            snapshot = make_snapshot(tms_id=tms_id, mission_id="msn_1")
            nouveaux = await service.filter_new_events(tms_id, [event])
            assert nouveaux == [event], f"événement perdu pour {tms_id}"
            report = await service.process_event_evaluations(
                snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
            )
            assert len(report.created) == 1

        assert await count(database, processed_events) == 2
        assert await count(database, alerts) == 2

    async def test_idempotence_toujours_active_au_sein_dun_tms(self, database, shipped_rules):
        """F7 : le TMS renvoie toute sa chronologie à chaque appel."""
        service = AlertService(database)
        snapshot = make_snapshot(tms_id="client_a", mission_id="msn_1")
        event = make_event(
            event_id="evt_crit",
            event_type=EventType.DRIVER_MESSAGE,
            payload={"severity": "critical", "text": "Accident"},
        )

        await service.process_event_evaluations(
            snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
        )

        assert await service.filter_new_events("client_a", [event]) == []
        assert await service.filter_new_events("client_b", [event]) == [event]


class TestCycleMultiTms:
    async def test_un_cycle_couvre_tous_les_tms(self, database, two_tms_mcp):
        """Le worker n'énumère pas les TMS : le serveur MCP les connaît."""
        cycle = MonitoringCycle(
            database, two_tms_mcp, RuleRepository(RULES_DIRECTORY), AlertService(database)
        )
        rapports = [await cycle.run_once(tms_id) for tms_id in ("client_a", "client_b")]

        assert all(rapport.failed is False for rapport in rapports)
        assert [rapport.missions for rapport in rapports] == [1, 1]
        assert [rapport.tms_id for rapport in rapports] == ["client_a", "client_b"]
        # Deux missions `msn_1` distinctes, donc les deux jeux d'alertes.
        assert sum(rapport.alerts_created for rapport in rapports) == 4
        assert {row["tms_id"] for row in await rows(database, alerts)} == {"client_a", "client_b"}

    async def test_un_tms_injoignable_ne_prive_pas_lautre(self, database, two_tms_mcp, tms_pair):
        """F4, étendu au multi-TMS : la panne d'un client est circonscrite."""
        first, _ = tms_pair
        first.fail("/health", 503)

        cycle = MonitoringCycle(
            database, two_tms_mcp, RuleRepository(RULES_DIRECTORY), AlertService(database)
        )
        panne = await cycle.run_once("client_a")
        sain = await cycle.run_once("client_b")

        # Le TMS en panne échoue seul ; l'autre est surveillé normalement.
        assert panne.failed is True
        assert sain.failed is False
        assert sain.missions == 1
        assert {row["tms_id"] for row in await rows(database, alerts)} == {"client_b"}

    async def test_notifications_portent_leur_provenance(self, database, two_tms_mcp, shipped_rules):
        """Un SMS reçu doit pouvoir être relié au TMS qui l'a provoqué."""
        channels = {channel: ConsoleChannel(channel) for channel in NotificationChannel}
        cycle = MonitoringCycle(
            database, two_tms_mcp, RuleRepository(RULES_DIRECTORY), AlertService(database)
        )
        for tms_id in ("client_a", "client_b"):
            await cycle.run_once(tms_id)

        await NotificationService(database, two_tms_mcp, channels).dispatch_once()

        provenances = {row["tms_id"] for row in await rows(database, notification_outbox)}
        assert provenances == {"client_a", "client_b"}
        envoyes = {m.metadata["tms_id"] for m in channels[NotificationChannel.SMS].sent}
        assert envoyes == {"client_a", "client_b"}
        assert await count(database, notifications) > 0


class TestDialectesDifferents:
    """Deux TMS de dialectes différents, raccordés en même temps."""

    @pytest.fixture
    async def mixed_mcp(self):
        x_tms = FakeTms()
        fleet = FakeFleetLink(transports=[transport_payload("T-77001")])
        connections = [
            TmsConnection.from_parts(
                RestApiTmsConnector("http://a.test/v1", client=x_tms.client(), retry_backoff_s=0.001),
                tms_id="x_tms",
            ),
            TmsConnection.from_parts(
                HttpTmsConnector(
                    "http://fleetlink.test",
                    load_named_dialect("fleetlink"),
                    client=fleet.transport(),
                    retry_backoff_s=0.001,
                ),
                tms_id="fleetlink",
            ),
        ]
        context = McpContext.for_connections(connections)
        server, _ = build_server(context=context)
        async with McpClient(server) as client:
            yield client
        await context.aclose()

    async def test_cycle_sur_deux_dialectes(self, database, mixed_mcp):
        cycle = MonitoringCycle(database, mixed_mcp, RuleRepository(RULES_DIRECTORY), AlertService(database))
        rapports = [await cycle.run_once(tms_id) for tms_id in ("x_tms", "fleetlink")]

        assert all(rapport.failed is False for rapport in rapports)
        assert [rapport.missions for rapport in rapports] == [1, 1]
        # FleetLink ne fournit pas l'écart : seul le retard déclenche chez lui.
        par_tms: dict[str, set[str]] = {}
        for row in await rows(database, alerts):
            par_tms.setdefault(row["tms_id"], set()).add(row["rule_id"])
        assert par_tms["x_tms"] == {"deviation_persistent", "eta_delay"}
        assert par_tms["fleetlink"] == {"eta_delay"}


class TestResolutionDuTms:
    async def test_tms_inconnu_refuse(self, two_tms_context):
        with pytest.raises(UnknownTms, match="TMS inconnu"):
            two_tms_context.connection("client_z")

    async def test_tms_inconnu_est_une_erreur_dargument(self, two_tms_mcp):
        """Réessayer n'y changera rien, et rien n'est à conclure sur la mission."""
        from services.monitoring.integrations.mcp_client import McpCallFailed

        with pytest.raises(McpCallFailed) as caught:
            await two_tms_mcp.get_mission_snapshot("client_z", "msn_1")

        assert caught.value.error.code.value == "INVALID_ARGUMENT"
        assert caught.value.retryable is False

    async def test_decouverte_des_tms(self, two_tms_mcp):
        """Corollaire d'un `tms_id` obligatoire : pouvoir les découvrir."""
        connected = await two_tms_mcp.list_tms()
        assert {tms.tms_id for tms in connected} == {"client_a", "client_b"}
        assert all(tms.verdict == "ok" for tms in connected)

    async def test_un_seul_tms_doit_aussi_etre_nomme(self, database):
        """Même avec un seul TMS : aucun appel implicite, par prévention.

        Un appelant habitué à ne pas nommer son TMS deviendrait un défaut le
        jour du second raccordement, et ce défaut-là est silencieux.
        """
        tms = FakeTms()
        connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
        context = McpContext.for_connector(connector)
        server, _ = build_server(context=context)
        async with McpClient(server) as client:
            harvest = await client.collect_cycle("x_tms")
        await context.aclose()

        assert list(harvest.tms_time) == ["x_tms"]
        assert len(harvest.snapshots) == 1


class TestAudit:
    async def test_journal_porte_la_provenance(self, database, shipped_rules):
        """F27 : sans elle, deux clients seraient indiscernables dans l'audit."""
        from database.schema import audit_log

        snapshot = make_snapshot(
            tms_id="client_b", mission_id="msn_1", deviation_km=7.2, deviation_minutes=25
        )
        await AlertService(database).process_state_evaluations(
            snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
        )

        entries = await rows(database, audit_log)
        assert entries
        assert {entry["tms_id"] for entry in entries} == {"client_b"}
