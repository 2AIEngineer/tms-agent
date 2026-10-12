"""Tests du moteur d'évaluation, joués sur les règles réellement livrées.

Les scénarios décrivent des situations d'exploitation ; les règles viennent de
`definitions/`. Un test qui casse signale soit une régression du moteur, soit
une règle qui ne couvre plus la situation qu'elle décrit.
"""

from datetime import timedelta

from services.monitoring.rules.engine import (
    collect_observed,
    evaluate_event_rules,
    evaluate_state_rules,
    triggered,
)
from shared.mapped_dto import (
    EventType,
    MissionStatus,
    NotificationChannel,
    Position,
    Severity,
    VehicleStatus,
)
from tests.factories import TMS_NOW, make_event, make_snapshot


def ids(evaluations):
    return [evaluation.rule_id for evaluation in evaluations]


class TestReglesDetat:
    def test_derive_persistante_declenche(self, shipped_rules):
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25)
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        assert ids(declenchees) == ["deviation_persistent"]
        alerte = declenchees[0]
        assert alerte.severity is Severity.HIGH
        assert alerte.rule_version == 2
        assert alerte.title == "Dérive d'itinéraire"
        assert "AB-123-CD" in alerte.message
        assert "7.2 km" in alerte.message
        assert alerte.deduplication_key == "deviation:msn_1"
        assert alerte.cooldown_minutes == 30

    def test_derive_sous_le_seuil_ne_declenche_pas(self, shipped_rules):
        """4 km pendant 25 min : sous le seuil de 5 km, donc rien."""
        snapshot = make_snapshot(deviation_km=4.0, deviation_minutes=25)
        assert triggered(evaluate_state_rules(shipped_rules.rules, snapshot)) == []

    def test_derive_trop_recente_ne_declenche_pas(self, shipped_rules):
        """La durée compte autant que la distance : un contournement bref est normal."""
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=10)
        assert triggered(evaluate_state_rules(shipped_rules.rules, snapshot)) == []

    def test_mission_terminee_ne_declenche_pas(self, shipped_rules):
        """Toutes les règles d'état exigent `in_progress`."""
        snapshot = make_snapshot(status=MissionStatus.COMPLETED, deviation_km=7.2, deviation_minutes=25)
        assert triggered(evaluate_state_rules(shipped_rules.rules, snapshot)) == []

    def test_retard_eta(self, shipped_rules):
        snapshot = make_snapshot(delay_minutes=90)
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        assert ids(declenchees) == ["eta_delay"]
        assert declenchees[0].severity is Severity.MEDIUM
        assert "90 minutes de retard" in declenchees[0].message

    def test_avance_ne_declenche_pas(self, shipped_rules):
        """Un `delay_minutes` négatif est une avance, pas un retard."""
        snapshot = make_snapshot(delay_minutes=-30)
        assert triggered(evaluate_state_rules(shipped_rules.rules, snapshot)) == []

    def test_immobilisation_anormale(self, shipped_rules):
        snapshot = make_snapshot(vehicle_status=VehicleStatus.STOPPED, last_event_age_minutes=150)
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        assert ids(declenchees) == ["immobilization_anomaly"]
        assert declenchees[0].severity is Severity.HIGH

    def test_absence_de_donnees_vehicule_en_mouvement(self, shipped_rules):
        snapshot = make_snapshot(vehicle_status=VehicleStatus.MOVING, last_event_age_minutes=200)
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        assert ids(declenchees) == ["data_gap_moving"]

    def test_vehicule_arrete_et_roulant_sexcluent(self, shipped_rules):
        """`immobilization_anomaly` et `data_gap_moving` ne peuvent pas être vraies ensemble."""
        arrete = triggered(
            evaluate_state_rules(
                shipped_rules.rules,
                make_snapshot(vehicle_status=VehicleStatus.STOPPED, last_event_age_minutes=200),
            )
        )
        roulant = triggered(
            evaluate_state_rules(
                shipped_rules.rules,
                make_snapshot(vehicle_status=VehicleStatus.MOVING, last_event_age_minutes=200),
            )
        )
        assert ids(arrete) == ["immobilization_anomaly"]
        assert ids(roulant) == ["data_gap_moving"]

    def test_tri_par_severite_decroissante(self, shipped_rules):
        """F9 : les déclenchements les plus graves sont traités d'abord."""
        snapshot = make_snapshot(
            deviation_km=7.2,  # high
            deviation_minutes=25,
            delay_minutes=90,  # medium
            vehicle_status=VehicleStatus.MOVING,
            last_event_age_minutes=200,  # medium
        )
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))

        severites = [evaluation.severity for evaluation in declenchees]
        assert severites == sorted(severites, reverse=True)
        assert declenchees[0].rule_id == "deviation_persistent"

    def test_regles_devenement_ignorees_en_evaluation_detat(self, shipped_rules):
        """Une règle d'événement ne doit jamais être réévaluée à chaque cycle (F7)."""
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25)
        evalues = {e.rule_id for e in evaluate_state_rules(shipped_rules.rules, snapshot)}
        assert evalues == {rule.id for rule in shipped_rules.state_rules}
        assert "critical_incident" not in evalues

    def test_regle_desactivee_non_evaluee(self, shipped_rules):
        rules = [
            rule.model_copy(update={"enabled": False}) if rule.id == "deviation_persistent" else rule
            for rule in shipped_rules.rules
        ]
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25)
        assert triggered(evaluate_state_rules(rules, snapshot)) == []


class TestReglesDevenement:
    def test_incident_critique_escalade_jusqua_la_direction(self, shipped_rules):
        snapshot = make_snapshot()
        event = make_event(
            event_id="EVT-99",
            event_type=EventType.DRIVER_MESSAGE,
            payload={"severity": "critical", "text": "Accident sur A6, véhicule immobilisé"},
        )
        declenchees = triggered(evaluate_event_rules(shipped_rules.rules, snapshot, event))

        assert ids(declenchees) == ["critical_incident"]
        alerte = declenchees[0]
        assert alerte.severity is Severity.CRITICAL
        assert alerte.event_id == "EVT-99"
        assert "Accident sur A6" in alerte.message
        assert [n.management_level for n in alerte.notify] == ["M+1", "M+2", "M+3", "M+4"]
        assert [n.channel for n in alerte.notify] == [
            NotificationChannel.IN_APP,
            NotificationChannel.SMS,
            NotificationChannel.CALL,
            NotificationChannel.EMAIL,
        ]

    def test_message_chauffeur_non_critique_ne_declenche_pas(self, shipped_rules):
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.DRIVER_MESSAGE,
            payload={"severity": "low", "text": "Pause déjeuner"},
        )
        assert triggered(evaluate_event_rules(shipped_rules.rules, snapshot, event)) == []

    def test_arrivee_hors_zone(self, shipped_rules):
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.ARRIVED_DELIVERY,
            position=Position(lat=45.80, lon=4.90),
            distance_to_target_km=3.4,
        )
        declenchees = triggered(evaluate_event_rules(shipped_rules.rules, snapshot, event))

        assert ids(declenchees) == ["arrival_out_of_zone"]
        assert declenchees[0].cooldown_minutes == 0  # un événement ne se répète pas

    def test_distance_non_calculee_ne_declenche_pas(self, shipped_rules):
        """`distance_to_target_km` est un champ dérivé : absent, la règle est fausse."""
        snapshot = make_snapshot()
        event = make_event(event_type=EventType.ARRIVED_DELIVERY, distance_to_target_km=None)
        assert triggered(evaluate_event_rules(shipped_rules.rules, snapshot, event)) == []

    def test_regles_detat_ignorees_en_evaluation_devenement(self, shipped_rules):
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25)
        event = make_event(event_type=EventType.DRIVER_MESSAGE, payload={"severity": "low"})
        evalues = {e.rule_id for e in evaluate_event_rules(shipped_rules.rules, snapshot, event)}
        assert evalues == {rule.id for rule in shipped_rules.event_rules}


class TestEvaluationsNonDeclenchees:
    def test_rendues_pour_permettre_la_cloture_auto(self, shipped_rules):
        """F13 : c'est l'évaluation devenue fausse qui autorise à clore l'alerte."""
        snapshot = make_snapshot(deviation_km=1.0, deviation_minutes=2)
        evaluations = evaluate_state_rules(shipped_rules.rules, snapshot)

        deviation = next(e for e in evaluations if e.rule_id == "deviation_persistent")
        assert deviation.matched is False
        assert deviation.auto_resolve is True
        assert deviation.severity is Severity.HIGH  # la gravité reste connue
        assert deviation.title is None  # mais rien n'a été rendu

    def test_declenchements_avant_non_declenchements(self, shipped_rules):
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25)
        evaluations = evaluate_state_rules(shipped_rules.rules, snapshot)
        matched = [e.matched for e in evaluations]
        assert matched == sorted(matched, reverse=True)


class TestObservedData:
    def test_contient_les_valeurs_declenchantes(self, shipped_rules):
        """F10 : sans les valeurs observées, personne ne peut vérifier l'alerte après coup."""
        snapshot = make_snapshot(deviation_km=7.234999, deviation_minutes=25)
        alerte = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))[0]

        assert alerte.observed_data["deviation.current_offset_km"] == 7.234999  # valeur exacte
        assert alerte.observed_data["deviation.duration_minutes"] == 25
        assert alerte.observed_data["mission.status"] is MissionStatus.IN_PROGRESS
        assert "deviation.vehicle_position" in alerte.observed_data  # champ `observe`

    def test_ignore_les_champs_absents(self, shipped_rules):
        rule = shipped_rules.by_id("deviation_persistent")
        observed = collect_observed(rule, {"mission": {"status": "in_progress"}})
        assert observed == {"mission.status": "in_progress"}


class TestHeureTms:
    def test_les_regles_temporelles_suivent_lhorloge_du_tms(self, shipped_rules):
        """F3 : avancer l'heure TMS doit suffire à faire vieillir le dernier événement.

        Le même événement, observé plus tard par le TMS, finit par déclencher
        l'immobilisation — sans qu'aucune horloge locale n'intervienne.
        """
        event = make_event(occurred_at=TMS_NOW)

        tot = make_snapshot(
            vehicle_status=VehicleStatus.STOPPED,
            events=[event],
            observed_at=TMS_NOW + timedelta(minutes=30),
        )
        tard = make_snapshot(
            vehicle_status=VehicleStatus.STOPPED,
            events=[event],
            observed_at=TMS_NOW + timedelta(minutes=150),
        )

        assert triggered(evaluate_state_rules(shipped_rules.rules, tot)) == []
        assert ids(triggered(evaluate_state_rules(shipped_rules.rules, tard))) == ["immobilization_anomaly"]

    def test_evaluated_at_est_lheure_dobservation(self, shipped_rules):
        snapshot = make_snapshot(observed_at=TMS_NOW)
        evaluation = evaluate_state_rules(shipped_rules.rules, snapshot)[0]
        assert evaluation.evaluated_at == TMS_NOW


class TestSnapshotPartiel:
    def test_partie_manquante_ne_leve_pas(self, shipped_rules):
        """F4 : un instantané partiel est signalé, il n'interrompt pas le cycle."""
        snapshot = make_snapshot(with_deviation=False, with_eta=False, snapshot_complete=False)
        evaluations = evaluate_state_rules(shipped_rules.rules, snapshot)

        assert triggered(evaluations) == []
        assert len(evaluations) == len(shipped_rules.state_rules)

    def test_mission_sans_vehicule(self, shipped_rules):
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25).model_copy(update={"vehicle": None})
        declenchees = triggered(evaluate_state_rules(shipped_rules.rules, snapshot))
        # La règle de dérive ne teste pas le véhicule : elle déclenche, et le
        # message signale la plaque manquante au lieu d'échouer.
        assert ids(declenchees) == ["deviation_persistent"]
        assert "n/a" in declenchees[0].message


def test_evaluation_deterministe(shipped_rules):
    """Deux évaluations des mêmes données donnent exactement la même séquence.

    Condition nécessaire pour que l'équipe métier puisse rejouer un scénario et
    comparer les résultats avant de déposer une règle modifiée.
    """
    snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25, delay_minutes=90)
    premiere = evaluate_state_rules(shipped_rules.rules, snapshot)
    seconde = evaluate_state_rules(shipped_rules.rules, snapshot)
    assert premiere == seconde
