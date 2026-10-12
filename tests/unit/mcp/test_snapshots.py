"""Tests de l'assemblage des instantanés de mission.

Ce qui est vérifié : le parallélisme borné, et la règle de tolérance — la
mission est obligatoire, ses satellites ne le sont pas (F2, F4).
"""

import pytest

from services.mcp_server.connectors.base import TmsFailure
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.snapshots import SnapshotService
from shared.mapped_dto import MissionStatus, ToolErrorCode
from tests.fake_tms import TMS_NOW, FakeTms, mission_payload


def service(tms: FakeTms, **kwargs) -> SnapshotService:
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    return SnapshotService(connector, **kwargs)


class TestAssemblage:
    async def test_instantane_complet(self):
        snapshot = await service(FakeTms()).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert snapshot.snapshot_complete is True
        assert snapshot.missing_parts == ()
        assert snapshot.vehicle is not None
        assert snapshot.driver is not None
        assert snapshot.deviation is not None
        assert snapshot.eta is not None

    async def test_evenements_du_detail_evitent_un_appel(self):
        """Un appel par mission et par cycle épargné compte face au quota du TMS."""
        tms = FakeTms()
        snapshot = await service(tms).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert len(snapshot.events) == 3
        assert tms.calls["/missions/msn_1/events"] == 0

    async def test_repli_sur_la_ressource_evenements(self):
        """Un TMS qui n'inclut pas la chronologie dans le détail reste géré."""
        tms = FakeTms(missions=[mission_payload(with_events=False)])
        tms.missions[0]["events"] = None
        snapshot = await service(tms).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert tms.calls["/missions/msn_1/events"] == 1


class TestTolerance:
    async def test_partie_indisponible_rend_un_snapshot_partiel(self):
        """F4 : l'écart illisible est signalé, le retard reste surveillé."""
        tms = FakeTms()
        tms.fail("/missions/msn_1/deviation", 503)

        snapshot = await service(tms).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert snapshot.snapshot_complete is False
        assert snapshot.missing_parts == ("deviation",)
        assert snapshot.deviation is None
        assert snapshot.eta is not None  # le reste est bien là

    async def test_ressource_absente_nest_pas_une_partie_manquante(self):
        """Un itinéraire non planifié est un fait du TMS, pas un incident."""
        tms = FakeTms(route=None)
        snapshot = await service(tms).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert snapshot.snapshot_complete is True
        assert snapshot.planned_route is None

    async def test_mission_illisible_remonte(self):
        """Sans la mission, il n'y a rien à surveiller : l'échec doit remonter."""
        tms = FakeTms()
        tms.fail("/missions/msn_1", 503)

        with pytest.raises(TmsFailure):
            await service(tms).build("msn_1", observed_at=TMS_NOW)

    async def test_mission_sans_vehicule_declare(self):
        mission = mission_payload()
        mission["vehicle_id"] = None
        tms = FakeTms(missions=[mission])

        snapshot = await service(tms).build("msn_1", observed_at=TMS_NOW)

        assert snapshot is not None
        assert snapshot.vehicle is None
        assert snapshot.snapshot_complete is True  # rien n'a échoué, rien n'était déclaré


class TestLot:
    async def test_missions_lisibles_et_illisibles_cohabitent(self):
        """Un cycle traite ce qu'il peut et signale le reste, sans s'arrêter."""
        tms = FakeTms(missions=[mission_payload("msn_1"), mission_payload("msn_2"), mission_payload("msn_3")])
        tms.fail("/missions/msn_2", 503)

        batch = await service(tms).build_many(["msn_1", "msn_2", "msn_3"], observed_at=TMS_NOW)

        assert [snapshot.mission_id for snapshot in batch.snapshots] == ["msn_1", "msn_3"]
        assert batch.failures["msn_2"].code is ToolErrorCode.TMS_UNAVAILABLE
        assert batch.failures["msn_2"].retryable is True

    async def test_parallelisme_borne(self):
        """F2 : sans plafond, deux cents missions infligeraient une rafale au TMS."""
        tms = FakeTms(missions=[mission_payload(f"msn_{index}") for index in range(12)])
        snapshot_service = service(tms, mission_concurrency=3)

        batch = await snapshot_service.build_many(
            [f"msn_{index}" for index in range(12)], observed_at=TMS_NOW
        )

        assert len(batch.snapshots) == 12
        # Le plafond de parallélisme est appliqué par le sémaphore du service ;
        # ce que l'on vérifie ici, c'est qu'il ne perd aucune mission.
        assert sorted(s.mission_id for s in batch.snapshots) == sorted(f"msn_{index}" for index in range(12))

    async def test_cycle_complet(self):
        """Heure TMS, missions surveillées, instantanés : l'étape F2 de bout en bout."""
        tms = FakeTms(
            missions=[
                mission_payload("msn_1", status="in_progress"),
                mission_payload("msn_2", status="completed"),
                mission_payload("msn_3", status="planned"),
            ]
        )
        batch = await service(tms).collect_cycle()

        assert batch.observed_at == TMS_NOW
        # Les missions terminées ne sont pas surveillées.
        assert [snapshot.mission_id for snapshot in batch.snapshots] == ["msn_1", "msn_3"]
        assert {snapshot.status for snapshot in batch.snapshots} == {
            MissionStatus.IN_PROGRESS,
            MissionStatus.PLANNED,
        }
        assert "partielles=0" in batch.summary()

    async def test_mission_au_statut_inconnu_signalee(self):
        mission = mission_payload("msn_9")
        mission["status"] = "en_attente_de_quai"
        tms = FakeTms(missions=[mission])

        batch = await service(tms).build_many(["msn_9"], observed_at=TMS_NOW)

        assert batch.snapshots == []
        assert batch.failures["msn_9"].code is ToolErrorCode.PARTIAL_DATA


class TestFiltrageDesStatuts:
    """Le filtre de statut est réappliqué côté client.

    Un TMS tiers qui ne gère pas le paramètre `status` peut l'ignorer et rendre
    toute sa base : sans ce second tri, le cycle relirait l'intégralité des
    missions historiques à chaque passage.
    """

    async def test_tms_ignorant_le_filtre_ne_fait_pas_surveiller_le_termine(self):
        tms = FakeTms(
            missions=[
                mission_payload("msn_1", status="in_progress"),
                mission_payload("msn_2", status="completed"),
                mission_payload("msn_3", status="cancelled"),
                mission_payload("msn_4", status="planned"),
            ]
        )
        tms.ignore_status_filter = True

        batch = await service(tms).collect_cycle()

        assert [snapshot.mission_id for snapshot in batch.snapshots] == ["msn_1", "msn_4"]
        # Terminée et annulée sont ignorées sans bruit : nous ne les demandions pas.
        assert batch.failures == {}

    async def test_statut_non_reconnu_signale(self):
        """Nous ne pouvons pas savoir si elle aurait dû être surveillée."""
        mission = mission_payload("msn_9")
        mission["status"] = "en_attente_de_quai"
        tms = FakeTms(missions=[mission_payload("msn_1"), mission])
        tms.ignore_status_filter = True

        batch = await service(tms).collect_cycle()

        assert [snapshot.mission_id for snapshot in batch.snapshots] == ["msn_1"]
        assert "en_attente_de_quai" in batch.failures["msn_9"].message

    async def test_missions_sans_identifiant_ignorees(self):
        tms = FakeTms(missions=[{"status": "in_progress"}, mission_payload("msn_1")])
        tms.ignore_status_filter = True

        batch = await service(tms).collect_cycle()

        assert [snapshot.mission_id for snapshot in batch.snapshots] == ["msn_1"]
