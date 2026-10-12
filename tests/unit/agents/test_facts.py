"""Tests des faits déterministes remis à l'enquêteur (F21).

Ce que ces faits doivent garantir : l'enquêteur n'a pas à *calculer* ce que
nous savons mesurer, et ne peut donc pas se tromper dessus. Un LLM à qui l'on
demande « depuis combien de temps le véhicule est-il arrêté ? » répond parfois
de travers avec assurance ; si le chiffre est dans son contexte initial, la
question ne se pose plus.
"""

from datetime import timedelta

from services.agents.investigator.facts import build_observed_facts
from shared.mapped_dto import Alert, Severity, VehicleStatus
from tests.factories import TMS_NOW, make_snapshot


def deviation_alert(**overrides) -> Alert:
    base = {
        "tms_id": "x_tms",
        "mission_id": "msn_1",
        "rule_id": "deviation_persistent",
        "rule_version": 2,
        "severity": Severity.HIGH,
        "category": "route_deviation",
        "title": "Dérive d'itinéraire",
        "message": "Le véhicule s'est écarté de 7.2 km.",
        "observed_data": {"deviation.current_offset_km": 7.2},
        "deduplication_key": "deviation:msn_1",
        "detected_at": TMS_NOW,
    }
    return Alert(**{**base, **overrides})


class TestFaitsDeBase:
    def test_les_valeurs_declenchantes_sont_reprises(self):
        """Le point de départ de toute vérification : pourquoi l'alerte existe."""
        facts = build_observed_facts(deviation_alert(), make_snapshot())

        assert facts["alert"]["rule_id"] == "deviation_persistent"
        assert facts["alert"]["rule_version"] == 2
        assert facts["alert"]["observed_data"]["deviation.current_offset_km"] == 7.2

    def test_heure_tms_et_delai_depuis_la_detection(self):
        """Mesuré en heure TMS : l'enquête tourne après coup, parfois longtemps."""
        snapshot = make_snapshot(observed_at=TMS_NOW + timedelta(minutes=12))
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert facts["tms_time"] == (TMS_NOW + timedelta(minutes=12)).isoformat()
        assert facts["mission"]["minutes_since_detection"] == 12.0

    def test_age_du_dernier_evenement(self):
        snapshot = make_snapshot(last_event_age_minutes=95)
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert facts["last_event"]["age_minutes"] == 95.0
        assert facts["event_count"] == 1

    def test_etat_du_vehicule(self):
        snapshot = make_snapshot(vehicle_status=VehicleStatus.STOPPED)
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert facts["vehicle"]["plate"] == "AB-123-CD"
        assert facts["vehicle"]["status"] == "stopped"

    def test_ecart_et_retard(self):
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25, delay_minutes=90)
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert facts["deviation"]["current_offset_km"] == 7.2
        assert facts["deviation"]["duration_minutes"] == 25.0
        assert facts["eta"]["delay_minutes"] == 90.0

    def test_chauffeur_nomme_sans_son_numero(self):
        """L'enquêteur ne compose pas lui-même : il demande un appel sous garde-fous."""
        facts = build_observed_facts(deviation_alert(), make_snapshot())

        assert facts["driver"]["name"] == "Awa Traoré"
        assert facts["driver"]["reachable_by_phone"] is True
        assert "phone" not in facts["driver"]
        assert "+336" not in str(facts)


class TestAbsences:
    def test_mission_disparue_signalee_et_non_comblee(self):
        """La mission a pu se terminer entre la détection et l'enquête.

        L'absence est dite explicitement : sans cela, l'enquêteur pourrait
        conclure de son silence que tout va bien.
        """
        facts = build_observed_facts(deviation_alert(), None)

        assert facts["mission_snapshot_available"] is False
        assert "Ne rien conclure" in facts["note"]
        assert "mission" not in facts

    def test_instantane_partiel_signale(self):
        snapshot = make_snapshot(with_deviation=False, snapshot_complete=False)
        snapshot = snapshot.model_copy(update={"missing_parts": ("deviation",)})
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert facts["mission"]["snapshot_complete"] is False
        assert facts["mission"]["missing_parts"] == ["deviation"]
        assert "deviation" not in facts

    def test_mission_sans_vehicule(self):
        snapshot = make_snapshot().model_copy(update={"vehicle": None})
        facts = build_observed_facts(deviation_alert(), snapshot)

        assert "vehicle" not in facts
        assert facts["mission_snapshot_available"] is True


class TestSerialisation:
    def test_tout_est_serialisable(self):
        """Les faits partent dans un prompt et en JSONB : aucun objet Python."""
        import json

        facts = build_observed_facts(deviation_alert(), make_snapshot(deviation_km=7.2, delay_minutes=90))
        assert json.loads(json.dumps(facts)) == facts

    def test_aucune_interpretation(self):
        """Ce module ne livre que des mesures : aucun verdict, aucune cause.

        Si une interprétation s'y glissait, le LLM hériterait d'une conclusion
        qu'il présenterait comme la sienne — et l'audit ne saurait plus qui a
        conclu quoi.
        """
        import json

        rendu = json.dumps(build_observed_facts(deviation_alert(), make_snapshot(deviation_km=7.2)))

        for mot in ("probable", "cause", "risque", "recommand"):
            assert mot not in rendu.lower()
