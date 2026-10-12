"""Tests du mapper TMS → contrat pivot, piloté par un dialecte.

Trois propriétés portent toute la valeur de cette frontière :
  - le vocabulaire d'un TMS devient celui des règles, sans perte ;
  - une donnée irrégulière est absorbée, elle ne fait pas tomber le snapshot ;
  - les correspondances viennent du **dialecte**, pas du code : les mêmes
    assertions valent pour tout TMS dont le dialecte est correctement écrit.

Ces tests tournent sur le dialecte livré du TMS de référence. Ceux de
`test_dialect_fleetlink.py` rejouent les mêmes situations sur un TMS dont
aucun nom ne coïncide avec le nôtre.
"""

from datetime import timedelta

import pytest

from services.mcp_server.connectors.dialect import load_named_dialect
from services.mcp_server.mappers import PivotMapper
from shared.mapped_dto import EventSource, EventType, MissionStatus, Position, VehicleStatus
from tests.fake_tms import PARIS, TMS_NOW, FakeTms, event_payload, iso, mission_payload

# Un seul mapper pour tout le module : le dialecte est immuable, et le
# reconstruire à chaque test relirait le fichier sans rien prouver de plus.
MAPPER = PivotMapper(load_named_dialect("x_tms"))

# Origine et destination de la mission du double, telles que le mapper les lit.
ORIGIN, DESTINATION = MAPPER.mission_places(mission_payload())


class TestTypesDevenements:
    @pytest.mark.parametrize("raw", [member.value for member in EventType if member is not EventType.OTHER])
    def test_types_connus_traduits(self, raw):
        event_type, raw_type = MAPPER.event_type(raw)
        assert event_type.value == raw
        assert raw_type is None

    def test_casse_et_espaces_tolerees(self):
        assert MAPPER.event_type("  ARRIVED_PICKUP  ")[0] is EventType.ARRIVED_PICKUP

    def test_type_inconnu_conserve_sans_etre_perdu(self):
        """Un TMS légèrement différent ne doit pas aveugler la surveillance."""
        event_type, raw_type = MAPPER.event_type("CUSTOM_BORDER_CROSSING")
        assert event_type is EventType.OTHER
        assert raw_type == "CUSTOM_BORDER_CROSSING"

    @pytest.mark.parametrize("raw", [None, "", "   ", 42, {}])
    def test_type_absurde_range_sous_other(self, raw):
        assert MAPPER.event_type(raw)[0] is EventType.OTHER


class TestEvenements:
    def test_evenement_complet(self):
        event = MAPPER.event(
            event_payload("evt_1", "driver_message", TMS_NOW, payload={"severity": "critical"}),
        )
        assert event is not None
        assert event.id == "evt_1"
        assert event.type is EventType.DRIVER_MESSAGE
        assert event.occurred_at == TMS_NOW
        assert event.payload == {"severity": "critical"}
        assert event.source is EventSource.TMS

    @pytest.mark.parametrize(
        "broken",
        [
            {"mission_id": "msn_1", "type": "arrived_pickup", "occurred_at": iso(TMS_NOW)},  # sans id
            {"id": "evt_1", "mission_id": "msn_1", "type": "arrived_pickup"},  # sans date
            {"id": "evt_1", "type": "arrived_pickup", "occurred_at": iso(TMS_NOW)},  # sans mission
        ],
    )
    def test_evenement_sans_identite_ou_date_ecarte(self, broken):
        """L'identifiant porte l'idempotence (F7), la date porte l'évaluation.

        Sans l'un des deux, l'événement serait réévalué sans fin ou daté de
        l'instant présent : mieux vaut l'écarter en le journalisant.
        """
        assert MAPPER.event(broken) is None

    def test_date_mal_formee_ecarte_levenement(self):
        raw = event_payload("evt_1", "arrived_pickup", TMS_NOW)
        raw["occurred_at"] = "hier matin"
        assert MAPPER.event(raw) is None

    def test_liste_tolerante_aux_lignes_corrompues(self):
        """Perdre toute une chronologie pour une ligne abîmée serait pire."""
        events = MAPPER.events(
            [
                event_payload("evt_1", "arrived_pickup", TMS_NOW),
                {"type": "cassé"},
                event_payload("evt_2", "departed_pickup", TMS_NOW + timedelta(minutes=5)),
            ],
            mission_id="msn_1",
        )
        assert [event.id for event in events] == ["evt_1", "evt_2"]

    def test_position_hors_bornes_ignoree_sans_perdre_levenement(self):
        raw = event_payload("evt_1", "arrived_pickup", TMS_NOW, lat=999.0, lon=0.0)
        event = MAPPER.event(raw)
        assert event is not None
        assert event.position is None


class TestDistanceDerivee:
    def test_arrivee_au_chargement_mesuree_depuis_lorigine(self):
        """Champ dérivé : le TMS ne le fournit pas, la règle hors zone en dépend."""
        raw = event_payload("evt_1", "arrived_pickup", TMS_NOW, lat=PARIS["lat"], lon=PARIS["lon"])
        event = MAPPER.event(raw, origin=ORIGIN, destination=DESTINATION)
        assert event is not None
        assert event.distance_to_target_km == 0.0

    def test_arrivee_a_la_livraison_mesuree_depuis_la_destination(self):
        raw = event_payload("evt_1", "arrived_delivery", TMS_NOW, lat=45.80, lon=4.90)
        event = MAPPER.event(raw, origin=ORIGIN, destination=DESTINATION)
        assert event is not None
        assert 5 < event.distance_to_target_km < 9

    def test_evenement_sans_etape_attendue(self):
        """Un message chauffeur n'a pas de point prévu : pas de distance inventée."""
        raw = event_payload("evt_1", "driver_message", TMS_NOW)
        event = MAPPER.event(raw, origin=ORIGIN, destination=DESTINATION)
        assert event is not None
        assert event.distance_to_target_km is None

    def test_position_absente_laisse_la_distance_vide(self):
        assert MAPPER.distance_to_target_km(None, ORIGIN) is None
        assert MAPPER.distance_to_target_km(Position(lat=48.9, lon=2.35), None) is None


class TestStatuts:
    @pytest.mark.parametrize("raw", [member.value for member in MissionStatus])
    def test_statuts_de_mission_connus(self, raw):
        assert MAPPER.mission_status(raw) == MissionStatus(raw)

    def test_statut_de_mission_inconnu_rend_none(self):
        """Toutes les règles d'état testent ce champ : le deviner serait pire.

        Contrairement à un type d'événement, un statut de mission ne peut pas
        être rangé dans un fourre-tout — on surveillerait, ou on ignorerait, une
        mission à tort.
        """
        assert MAPPER.mission_status("en_attente_de_quai") is None

    def test_statut_vehicule_inconnu_se_replie(self):
        """`unknown` rend simplement les règles fausses, ce qui est acceptable ici."""
        assert MAPPER.vehicle_status("en_panne") is VehicleStatus.UNKNOWN
        assert MAPPER.vehicle_status("moving") is VehicleStatus.MOVING


class TestSnapshot:
    def test_assemblage_complet(self):
        tms = FakeTms()
        snapshot = MAPPER.mission_snapshot(
            mission_payload(),
            observed_at=TMS_NOW,
            vehicle=tms.vehicle,
            driver=tms.driver,
            deviation=tms.deviation,
            eta=tms.eta,
            route=tms.route,
        )

        assert snapshot is not None
        assert snapshot.mission_id == "msn_1"
        assert snapshot.status is MissionStatus.IN_PROGRESS
        assert snapshot.observed_at == TMS_NOW
        assert snapshot.snapshot_complete is True
        assert snapshot.vehicle is not None and snapshot.vehicle.plate == "AB-123-CD"
        assert snapshot.driver is not None and snapshot.driver.phone == "+33600000001"
        assert snapshot.deviation is not None and snapshot.deviation.current_offset_km == 7.234
        assert snapshot.eta is not None and snapshot.eta.delay_minutes == 90.0
        assert snapshot.planned_route is not None and len(snapshot.planned_route.waypoints) == 2
        assert len(snapshot.events) == 3

    def test_etapes_construites_depuis_origine_et_destination(self):
        """Le TMS décrit des lieux et des dates séparés ; le pivot en fait des étapes."""
        snapshot = MAPPER.mission_snapshot(mission_payload(), observed_at=TMS_NOW)
        assert snapshot is not None
        assert snapshot.loading is not None
        assert snapshot.loading.place.label == "Entrepôt Paris Nord"
        assert snapshot.loading.planned_at == TMS_NOW - timedelta(hours=3)
        assert snapshot.unloading is not None
        assert snapshot.unloading.place.label == "Plateforme Lyon Sud"

    def test_position_courante_vient_du_vehicule(self):
        """Une seule source, pour que deux champs ne puissent pas se contredire."""
        tms = FakeTms()
        snapshot = MAPPER.mission_snapshot(mission_payload(), observed_at=TMS_NOW, vehicle=tms.vehicle)
        assert snapshot is not None
        assert snapshot.current_position == snapshot.vehicle.current_position

    def test_parties_manquantes_signalees_sans_annuler_le_snapshot(self):
        """F4 : un écart illisible ne doit pas aveugler les règles de retard."""
        snapshot = MAPPER.mission_snapshot(
            mission_payload(),
            observed_at=TMS_NOW,
            deviation=None,
            missing_parts=("deviation", "route"),
        )
        assert snapshot is not None
        assert snapshot.snapshot_complete is False
        assert snapshot.missing_parts == ("deviation", "route")
        assert snapshot.deviation is None

    def test_mission_sans_identifiant_ecartee(self):
        assert MAPPER.mission_snapshot({"status": "in_progress"}, observed_at=TMS_NOW) is None

    def test_mission_au_statut_inconnu_ecartee(self):
        assert MAPPER.mission_snapshot({"id": "msn_1", "status": "???"}, observed_at=TMS_NOW) is None

    def test_reference_absente_se_replie_sur_lidentifiant(self):
        """La référence apparaît dans les messages d'alerte : jamais vide."""
        mission = mission_payload()
        del mission["reference"]
        snapshot = MAPPER.mission_snapshot(mission, observed_at=TMS_NOW)
        assert snapshot is not None
        assert snapshot.reference == "msn_1"

    def test_vehicule_sans_plaque_se_replie_sur_lidentifiant(self):
        vehicle = MAPPER.vehicle({"id": "veh_9", "status": "moving"})
        assert vehicle is not None
        assert vehicle.plate == "veh_9"

    def test_evenements_de_la_ressource_dediee_prioritaires(self):
        """Un TMS qui n'inclut pas les événements dans le détail reste géré."""
        mission = mission_payload(with_events=False)
        snapshot = MAPPER.mission_snapshot(
            mission,
            observed_at=TMS_NOW,
            events=[event_payload("evt_99", "arrived_delivery", TMS_NOW)],
        )
        assert snapshot is not None
        assert [event.id for event in snapshot.events] == ["evt_99"]


class TestTolerance:
    @pytest.mark.parametrize(
        "raw",
        [None, {}, {"lat": None, "lon": 2.0}, {"lat": "abc", "lon": 2.0}, {"lat": 95, "lon": 2.0}, "48,2"],
    )
    def test_position_inexploitable_rend_none(self, raw):
        assert MAPPER.position(raw) is None

    def test_itineraire_ignore_les_points_abimes(self):
        route = MAPPER.route(
            {
                "waypoints": [{"lat": 48.9, "lon": 2.3}, {"lat": None, "lon": 2.4}],
                "distance_km": "465.2",
                "duration_min": 300,
            }
        )
        assert route is not None
        assert len(route.waypoints) == 1
        assert route.distance_km == 465.2  # une chaîne numérique est acceptée
        assert route.duration_minutes == 300.0

    def test_booleen_non_confondu_avec_un_nombre(self):
        """`True` vaut 1 en Python : l'accepter masquerait une donnée corrompue."""
        route = MAPPER.route({"waypoints": [], "distance_km": True})
        assert route is not None
        assert route.distance_km is None
