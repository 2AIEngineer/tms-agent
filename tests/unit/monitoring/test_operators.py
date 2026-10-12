"""Tests des opérateurs du langage de règles.

Deux propriétés sont vérifiées partout, parce qu'elles portent la résilience du
cycle de surveillance :
  - un champ absent ou d'un type inattendu rend la condition fausse, sans lever ;
  - une valeur invalide dans le YAML est refusée au chargement, pas à l'exécution.
"""

from datetime import UTC, datetime, timedelta

import pytest

from services.monitoring.rules.operators import MISSING, InvalidRuleValue, get
from services.monitoring.rules.operators.geospatial import haversine_km, point_in_polygon

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


class TestComparaison:
    @pytest.mark.parametrize(
        ("operator", "observed", "expected", "result"),
        [
            ("equals", "in_progress", "in_progress", True),
            ("equals", "completed", "in_progress", False),
            ("not_equals", "completed", "in_progress", True),
            ("greater_than", 7.2, 5, True),
            ("greater_than", 5, 5, False),
            ("greater_or_equal", 5, 5, True),
            ("less_than", 3, 5, True),
            ("less_or_equal", 5, 5, True),
            ("between", 7, [5, 10], True),
            ("between", 11, [5, 10], False),
            ("between", 5, [5, 10], True),
            ("in", "arrived_pickup", ["arrived_pickup", "arrived_delivery"], True),
            ("in", "loading_started", ["arrived_pickup"], False),
            ("not_in", "loading_started", ["arrived_pickup"], True),
        ],
    )
    def test_verdicts(self, operator, observed, expected, result):
        assert get(operator).apply(observed, expected, NOW) is result

    @pytest.mark.parametrize(
        "operator",
        ["equals", "not_equals", "greater_than", "less_than", "between", "in", "not_in"],
    )
    def test_champ_absent_rend_faux(self, operator):
        """Un champ absent du contexte ne déclenche jamais, quel que soit l'opérateur.

        `not_equals` compris : « différent de X » ne doit pas devenir vrai parce
        que le TMS a cessé de renseigner le champ, sinon une panne de lecture
        déclencherait des alertes en masse.
        """
        value = [1, 2] if operator in ("between", "in", "not_in") else 1
        assert get(operator).apply(MISSING, value, NOW) is False

    def test_valeur_nulle_rend_faux(self):
        assert get("greater_than").apply(None, 5, NOW) is False

    def test_type_inattendu_rend_faux_sans_lever(self):
        assert get("greater_than").apply("pas un nombre", 5, NOW) is False

    def test_booleen_refuse_comme_seuil_numerique(self):
        """`True` vaut 1 en Python : l'accepter comme seuil masquerait une faute."""
        with pytest.raises(InvalidRuleValue):
            get("greater_than").validate(True)

    @pytest.mark.parametrize("bad", ["cinq", None, [1, 2]])
    def test_seuil_non_numerique_refuse(self, bad):
        with pytest.raises(InvalidRuleValue):
            get("greater_than").validate(bad)

    def test_between_bornes_inversees_refusees(self):
        with pytest.raises(InvalidRuleValue, match="borne basse"):
            get("between").validate([10, 5])

    @pytest.mark.parametrize("bad", [[], "abc", 5])
    def test_in_exige_une_liste_non_vide(self, bad):
        with pytest.raises(InvalidRuleValue):
            get("in").validate(bad)


class TestTemporel:
    def test_older_than_minutes(self):
        il_y_a_trois_heures = NOW - timedelta(hours=3)
        assert get("older_than_minutes").apply(il_y_a_trois_heures, 120, NOW) is True
        assert get("older_than_minutes").apply(il_y_a_trois_heures, 240, NOW) is False

    def test_newer_than_minutes(self):
        il_y_a_dix_minutes = NOW - timedelta(minutes=10)
        assert get("newer_than_minutes").apply(il_y_a_dix_minutes, 30, NOW) is True
        assert get("newer_than_minutes").apply(il_y_a_dix_minutes, 5, NOW) is False

    def test_date_naive_supposee_utc(self):
        """Un TMS qui rend une date sans fuseau ne doit pas faire tomber le cycle."""
        naive = datetime(2026, 10, 10, 9, 0)
        assert get("older_than_minutes").apply(naive, 120, NOW) is True

    @pytest.mark.parametrize(
        ("observed", "result"),
        [
            (25.0, True),  # minutes déjà calculées par le TMS
            (10.0, False),
            (timedelta(minutes=25), True),  # durée native
            (NOW - timedelta(minutes=25), True),  # instant de départ
            (NOW - timedelta(minutes=5), False),
        ],
    )
    def test_duration_greater_than_accepte_les_trois_formes(self, observed, result):
        assert get("duration_greater_than_minutes").apply(observed, 20, NOW) is result

    def test_heure_tms_et_non_horloge_locale(self):
        """Le verdict suit `now`, fourni par le snapshot, et rien d'autre.

        C'est la garantie qui permet au simulateur d'accélérer ou de remettre à
        zéro son horloge sans fausser les règles temporelles (F3).
        """
        moment = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        tms_now = moment + timedelta(hours=5)
        assert get("older_than_minutes").apply(moment, 60, tms_now) is True
        assert get("older_than_minutes").apply(moment, 60, moment) is False

    def test_seuil_negatif_refuse(self):
        with pytest.raises(InvalidRuleValue, match="négatif"):
            get("older_than_minutes").validate(-5)


class TestGeospatial:
    def test_haversine_paris_lyon(self):
        """Référence connue : Paris-Lyon vaut environ 392 km à vol d'oiseau."""
        distance = haversine_km(48.8566, 2.3522, 45.7640, 4.8357)
        assert 390 < distance < 395

    def test_distance_greater_than_km(self):
        operator = get("distance_greater_than_km")
        cible = {"lat": 48.8566, "lon": 2.3522, "km": 2}
        assert operator.apply({"lat": 48.8566, "lon": 2.3522}, cible, NOW) is False
        assert operator.apply({"lat": 45.7640, "lon": 4.8357}, cible, NOW) is True

    def test_position_absente_rend_faux(self):
        cible = {"lat": 48.8566, "lon": 2.3522, "km": 2}
        assert get("distance_greater_than_km").apply(MISSING, cible, NOW) is False
        assert get("distance_greater_than_km").apply({"lat": None, "lon": None}, cible, NOW) is False

    @pytest.mark.parametrize(
        "bad",
        [
            {"lat": 48.8, "lon": 2.3},  # 'km' manquant
            {"lat": 48.8, "km": 2},  # 'lon' manquant
            {"lat": 200, "lon": 2.3, "km": 2},  # latitude hors bornes
            "48.8,2.3",
        ],
    )
    def test_cible_invalide_refusee(self, bad):
        with pytest.raises(InvalidRuleValue):
            get("distance_greater_than_km").validate(bad)

    def test_point_in_polygon(self):
        carre = [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0]]
        assert point_in_polygon(5.0, 5.0, carre) is True
        assert point_in_polygon(15.0, 5.0, carre) is False

    def test_inside_et_outside_polygon(self):
        zone = [[48.0, 2.0], [48.0, 3.0], [49.0, 3.0], [49.0, 2.0]]
        dedans = {"lat": 48.5, "lon": 2.5}
        dehors = {"lat": 45.0, "lon": 5.0}
        assert get("inside_polygon").apply(dedans, zone, NOW) is True
        assert get("inside_polygon").apply(dehors, zone, NOW) is False
        assert get("outside_polygon").apply(dehors, zone, NOW) is True
        assert get("outside_polygon").apply(dedans, zone, NOW) is False

    def test_position_inconnue_nest_pas_une_sortie_de_zone(self):
        """Une absence de donnée GPS ne doit pas valoir « hors zone »."""
        zone = [[48.0, 2.0], [48.0, 3.0], [49.0, 3.0], [49.0, 2.0]]
        assert get("outside_polygon").apply(MISSING, zone, NOW) is False

    @pytest.mark.parametrize("bad", [[[0, 0], [1, 1]], [[0, 0, 0], [1, 1], [2, 2]], "zone"])
    def test_polygone_invalide_refuse(self, bad):
        with pytest.raises(InvalidRuleValue):
            get("inside_polygon").validate(bad)


def test_operateur_inconnu_signale_les_operateurs_disponibles():
    """Le message d'erreur doit servir à l'équipe métier, pas au développeur."""
    with pytest.raises(InvalidRuleValue, match="Opérateurs disponibles"):
        get("plus_grand_que")
