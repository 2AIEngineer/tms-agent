"""Tests du rendu des gabarits de message.

Deux enjeux : des messages lisibles par un chauffeur ou un superviseur, et
l'impossibilité d'exécuter quoi que ce soit depuis un fichier de règle.
"""

from datetime import UTC, datetime

import pytest

from services.monitoring.rules.template import MISSING_PLACEHOLDER, format_value, render

CONTEXT = {
    "mission": {"id": "MSN-2", "reference": "REF-0002"},
    "vehicle": {"plate": "AB-123-CD"},
    "deviation": {"current_offset_km": 7.234999, "duration_minutes": 25.0},
    "eta": {"current_eta": datetime(2026, 10, 10, 14, 30, tzinfo=UTC), "delay_minutes": None},
    "event": {"payload": {"text": "Pneu crevé sur A6"}},
}


class TestRender:
    def test_substitution_simple(self):
        assert render("Mission {{ mission.reference }}", CONTEXT) == "Mission REF-0002"

    def test_chemin_profond(self):
        assert render("{{ event.payload.text }}", CONTEXT) == "Pneu crevé sur A6"

    def test_espaces_dans_les_accolades_toleres(self):
        assert render("{{mission.id}} {{   mission.id   }}", CONTEXT) == "MSN-2 MSN-2"

    def test_champ_absent_rendu_explicite(self):
        """Mieux vaut un « n/a » diagnosticable qu'un trou blanc dans un SMS."""
        assert render("Retard {{ eta.delay_minutes }} min", CONTEXT) == f"Retard {MISSING_PLACEHOLDER} min"
        assert render("{{ inexistant.champ }}", CONTEXT) == MISSING_PLACEHOLDER

    def test_message_multiligne_ramene_sur_une_ligne(self):
        """Les messages partent en SMS, en in-app ou en appel vocal : pas de mise en page."""
        gabarit = (
            "Le véhicule {{ vehicle.plate }}\n  s'est écarté de\n  {{ deviation.current_offset_km }} km."
        )
        assert render(gabarit, CONTEXT) == "Le véhicule AB-123-CD s'est écarté de 7.23 km."

    def test_aucune_execution_de_code(self):
        """Les règles sont rechargées à chaud : rien d'exécutable ne doit y entrer."""
        for dangereux in [
            "{{ __import__('os').system('rm -rf /') }}",
            "{% for x in y %}{% endfor %}",
            "{{ 7*7 }}",
            "${mission.id}",
        ]:
            rendu = render(dangereux, CONTEXT)
            assert "49" not in rendu
            assert rendu == " ".join(dangereux.split()) or MISSING_PLACEHOLDER in rendu


class TestFormatValue:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (7.234999, "7.23"),  # la précision du TMS est inutile dans un message
            (5.0, "5"),  # pas de « 5.0 km »
            (25, "25"),
            (None, MISSING_PLACEHOLDER),
            (True, "oui"),
            (False, "non"),
            ("in_progress", "in_progress"),
            (datetime(2026, 10, 10, 14, 30, tzinfo=UTC), "2026-10-10T14:30+00:00"),
        ],
    )
    def test_mise_en_forme(self, value, expected):
        assert format_value(value) == expected

    def test_donnee_brute_conservee_ailleurs(self):
        """L'arrondi ne concerne que l'affichage : `observed_data` garde la valeur exacte."""
        assert format_value(7.234999) == "7.23"
