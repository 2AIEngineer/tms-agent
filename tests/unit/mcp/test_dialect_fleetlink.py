"""Le test décisif : un TMS dont aucun nom ne coïncide avec le nôtre.

FleetLink est fictif, et pris à dessein en contre-pied du TMS de référence :
préfixe d'URL, noms d'endpoints, noms de champs, imbrication, enveloppes de
réponse, pagination par numéro de page, authentification par en-tête, statuts
en majuscules et plus granulaires — et une ressource purement absente.

Aucune ligne de code n'a été écrite pour lui : seulement son dialecte. Si les
mêmes situations métier passent ici et sur le TMS de référence, alors brancher
un TMS est bien devenu un travail de configuration.
"""

import pytest

from services.mcp_server.connectors.dialect import Operation, load_named_dialect
from services.mcp_server.connectors.http_tms import HttpTmsConnector
from services.mcp_server.mappers import PivotMapper
from services.mcp_server.snapshots import SnapshotService
from services.monitoring.rules.engine import evaluate_state_rules, triggered
from shared.mapped_dto import EventSource, EventType, MissionStatus, VehicleStatus
from tests.fake_fleetlink import FLEET_NOW, FakeFleetLink, timeline_payload, transport_payload

DIALECT = load_named_dialect("fleetlink")
MAPPER = PivotMapper(DIALECT)


def connector(fleet: FakeFleetLink, **kwargs) -> HttpTmsConnector:
    return HttpTmsConnector(
        "http://fleetlink.test",
        DIALECT,
        client=fleet.transport(),
        api_key="clef-de-test",
        retry_backoff_s=0.001,
        **kwargs,
    )


def service(fleet: FakeFleetLink, **kwargs) -> SnapshotService:
    return SnapshotService(connector(fleet), MAPPER, **kwargs)  # type: ignore[arg-type]


class TestTransport:
    async def test_chemins_du_dialecte_et_non_les_notres(self):
        """Les URL appelées sont celles de FleetLink, jamais les nôtres."""
        fleet = FakeFleetLink()
        await service(fleet).collect_cycle()

        appeles = set(fleet.calls)
        assert "/resources/transports" in appeles
        assert "/resources/transports/T-77001/way" in appeles
        assert "/resources/transports/T-77001/forecast" in appeles
        # Aucun chemin du TMS de référence n'a fui.
        assert not any(path.startswith("/missions") for path in appeles)
        assert not any(path.endswith("/route") for path in appeles)

    async def test_authentification_par_en_tete_declaree(self):
        fleet = FakeFleetLink()
        await connector(fleet).tms_time()
        assert fleet.api_keys == ["clef-de-test"]

    async def test_enveloppe_de_reponse_desenveloppee(self):
        """`{"result": {...}}` est décrit par `result_path`, pas par du code."""
        fleet = FakeFleetLink()
        mission = await connector(fleet).get_mission("T-77001")
        assert mission["mss_id"] == "T-77001"  # et non {"result": {...}}

    async def test_pagination_par_numero_de_page(self):
        """Style différent du curseur, sans que le connecteur le sache."""
        fleet = FakeFleetLink(transports=[transport_payload(f"T-{index}") for index in range(3)])
        missions = await connector(fleet).list_missions()
        assert len(missions) == 3

    async def test_statuts_traduits_dans_le_vocabulaire_du_tms(self):
        """Envoyer `in_progress` à FleetLink ne filtrerait rien.

        Le connecteur traduit nos statuts canoniques vers les siens avant
        d'envoyer le filtre.
        """
        fleet = FakeFleetLink(
            transports=[
                transport_payload("T-1", state="RUNNING"),
                transport_payload("T-2", state="AT_PICKUP"),
                transport_payload("T-3", state="DONE"),
                transport_payload("T-4", state="SCHEDULED"),
            ]
        )
        missions = await connector(fleet).list_missions(
            statuses=[MissionStatus.PLANNED.value, MissionStatus.IN_PROGRESS.value]
        )
        # RUNNING et AT_PICKUP se replient tous deux sur in_progress.
        assert {m["mss_id"] for m in missions} == {"T-1", "T-2", "T-4"}

    async def test_lecture_seule(self):
        fleet = FakeFleetLink()
        await service(fleet).collect_cycle()
        assert set(fleet.methods) == {"GET"}


class TestTraduction:
    async def test_instantane_complet_depuis_un_vocabulaire_etranger(self):
        """Le cœur du test : `mss_id`, `from.y`, `tier` deviennent le pivot."""
        fleet = FakeFleetLink()
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        assert snapshot.mission_id == "T-77001"
        assert snapshot.reference == "FL-2026-77001"
        assert snapshot.status is MissionStatus.IN_PROGRESS

        assert snapshot.vehicle is not None
        assert snapshot.vehicle.plate == "AB-123-CD"
        assert snapshot.vehicle.status is VehicleStatus.MOVING
        assert snapshot.vehicle.current_position is not None
        assert snapshot.vehicle.current_position.speed_kmh == 82.0

        assert snapshot.driver is not None
        assert snapshot.driver.name == "Awa Traoré"
        assert snapshot.driver.phone == "+33600000001"  # contact.mobile
        assert snapshot.driver.management_level == "M+0"  # tier

        assert snapshot.loading is not None
        assert snapshot.loading.place.label == "Entrepôt Paris Nord"  # from.name
        assert snapshot.loading.place.lat == 48.9014  # from.y

        assert snapshot.eta is not None
        assert snapshot.eta.delay_minutes == 90.0  # lateness_min
        assert snapshot.eta.remaining_distance_km == 310.5  # left_km

        assert snapshot.planned_route is not None
        assert len(snapshot.planned_route.waypoints) == 2  # geometry.points
        assert snapshot.planned_route.duration_minutes == 300.0  # total_min

    async def test_chronologie_traduite(self):
        fleet = FakeFleetLink()
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        assert [event.type for event in snapshot.events] == [
            EventType.ARRIVED_PICKUP,  # PICKUP_ARRIVAL
            EventType.LOADING_COMPLETED,  # LOAD_END
            EventType.DEPARTED_PICKUP,  # PICKUP_DEPARTURE
        ]
        assert all(event.source is EventSource.TMS for event in snapshot.events)  # BACKOFFICE

    async def test_plusieurs_valeurs_du_tms_vers_une_des_notres(self):
        """`DRIVER_NOTE` et `DRIVER_ALERT` sont tous deux un message chauffeur."""
        assert MAPPER.event_type("DRIVER_NOTE")[0] is EventType.DRIVER_MESSAGE
        assert MAPPER.event_type("DRIVER_ALERT")[0] is EventType.DRIVER_MESSAGE

    async def test_dates_suffixees_z_acceptees(self):
        fleet = FakeFleetLink()
        moment = await connector(fleet).tms_time()
        assert moment == FLEET_NOW

    async def test_distance_derivee_calculee_comme_ailleurs(self):
        """Champ dérivé : du code, identique quel que soit le TMS."""
        fleet = FakeFleetLink()
        fleet.transports[0]["timeline"] = [
            timeline_payload("E-9", "DROP_ARRIVAL", FLEET_NOW, lat=45.80, lon=4.90)
        ]
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        distance = snapshot.events[0].distance_to_target_km
        assert distance is not None and 5 < distance < 9


class TestRessourceAbsente:
    async def test_operation_declaree_absente_nest_pas_appelee(self):
        """`get_deviation: unsupported` évite un appel voué à l'échec."""
        fleet = FakeFleetLink()
        assert await connector(fleet).get_deviation("T-77001") is None
        assert not any("deviation" in path for path in fleet.calls)

    async def test_absence_declaree_ne_rend_pas_le_snapshot_partiel(self):
        """Une capacité absente n'est pas un incident : rien n'a échoué.

        La distinction compte : `missing_parts` sert à ne pas clore une alerte
        sur une lecture ratée (F14). Y ranger une ressource que ce TMS n'aura
        jamais empêcherait toute clôture automatique, à chaque cycle, pour
        toujours.
        """
        fleet = FakeFleetLink()
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        assert snapshot.deviation is None
        assert snapshot.missing_parts == ()
        assert snapshot.snapshot_complete is True

    async def test_regles_dependant_de_labsence_ne_declenchent_pas(self, shipped_rules):
        """Sans écart, la règle de dérive est fausse — elle n'invente rien."""
        fleet = FakeFleetLink()
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        declenchees = {e.rule_id for e in triggered(evaluate_state_rules(shipped_rules.rules, snapshot))}
        assert "deviation_persistent" not in declenchees


class TestReglesInchangees:
    async def test_les_regles_livrees_declenchent_sur_fleetlink(self, shipped_rules):
        """Aucune règle n'a été modifiée pour ce TMS, et le retard est détecté."""
        fleet = FakeFleetLink()
        snapshot = await service(fleet).build("T-77001", observed_at=FLEET_NOW)

        assert snapshot is not None
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        assert [e.rule_id for e in declenchees] == ["eta_delay"]
        assert "90 minutes de retard" in declenchees[0].message
        assert "AB-123-CD" in declenchees[0].message
        assert declenchees[0].deduplication_key == "eta_delay:T-77001"

    async def test_cycle_complet(self, shipped_rules):
        fleet = FakeFleetLink(
            transports=[
                transport_payload("T-1", state="RUNNING"),
                transport_payload("T-2", state="DONE"),
                transport_payload("T-3", state="BOOKED"),
            ]
        )
        batch = await service(fleet).collect_cycle()

        assert [s.mission_id for s in batch.snapshots] == ["T-1", "T-3"]
        assert batch.failures == {}
        assert batch.observed_at == FLEET_NOW


class TestEquivalenceAvecLeTmsDeReference:
    """Les deux dialectes doivent produire le même pivot sur la même situation.

    C'est la formulation la plus directe de la promesse : le reste du système
    ne doit pas pouvoir distinguer les deux TMS.
    """

    async def test_meme_pivot_sur_une_mission_equivalente(self):
        from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
        from tests.fake_tms import FakeTms

        reference = SnapshotService(
            RestApiTmsConnector("http://tms.test/v1", client=FakeTms().client()),
            PivotMapper(load_named_dialect("x_tms")),
        )
        fleetlink = service(FakeFleetLink())

        a = await reference.build("msn_1", observed_at=FLEET_NOW)
        b = await fleetlink.build("T-77001", observed_at=FLEET_NOW)

        assert a is not None and b is not None
        # Les identifiants diffèrent, le reste du pivot se superpose.
        assert a.status is b.status
        assert a.vehicle is not None and b.vehicle is not None
        assert a.vehicle.plate == b.vehicle.plate
        assert a.vehicle.status is b.vehicle.status
        assert a.driver is not None and b.driver is not None
        assert (a.driver.name, a.driver.phone) == (b.driver.name, b.driver.phone)
        assert a.loading is not None and b.loading is not None
        assert a.loading.place == b.loading.place
        assert a.eta is not None and b.eta is not None
        assert a.eta.delay_minutes == b.eta.delay_minutes
        assert [e.type for e in a.events] == [e.type for e in b.events]


class TestOperationsDuDialecte:
    @pytest.mark.parametrize("operation", list(Operation))
    def test_chaque_operation_a_une_correspondance_ou_est_declaree_absente(self, operation):
        """La validation au chargement l'exige : ce test documente la règle."""
        assert operation in DIALECT.operations
