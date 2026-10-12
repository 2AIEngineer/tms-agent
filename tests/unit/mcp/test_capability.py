"""Tests du rapport de capacité.

Ce que le rapport doit garantir : quand un TMS ne peut pas fournir une donnée,
les règles qui en dépendent sont **nommées**. Une alerte qui ne part jamais
ressemble en tout point à une flotte sans anomalie ; c'est ce rapport qui fait
la différence entre les deux.
"""

import pytest

from services.mcp_server.capability import Verdict, build_report, rule_status
from services.mcp_server.connectors.dialect import Operation, load_named_dialect


@pytest.fixture
def x_tms():
    return load_named_dialect("x_tms")


@pytest.fixture
def fleetlink():
    return load_named_dialect("fleetlink")


class TestTmsComplet:
    def test_verdict_complet(self, x_tms, shipped_rules):
        report = build_report(x_tms, shipped_rules)
        assert report.verdict is Verdict.OK
        assert report.inoperative_rules == []
        assert report.required_from_vendor() == []

    def test_toutes_les_capacites_offertes(self, x_tms, shipped_rules):
        report = build_report(x_tms, shipped_rules)
        assert all(feature.available for feature in report.features)


class TestTmsDegrade:
    def test_verdict_degrade(self, fleetlink, shipped_rules):
        report = build_report(fleetlink, shipped_rules)
        assert report.verdict is Verdict.DEGRADED

    def test_regle_inoperante_nommee_avec_ses_champs(self, fleetlink, shipped_rules):
        """Le cœur du rapport : dire *quelle* règle et *pourquoi*."""
        report = build_report(fleetlink, shipped_rules)

        inoperantes = {rule.rule_id: rule for rule in report.inoperative_rules}
        assert set(inoperantes) == {"deviation_persistent"}
        derive = inoperantes["deviation_persistent"]
        assert derive.missing_operations == ("get_deviation",)
        assert "deviation.current_offset_km" in derive.unavailable_fields

    def test_les_autres_regles_restent_operationnelles(self, fleetlink, shipped_rules):
        report = build_report(fleetlink, shipped_rules)
        operationnelles = {rule.rule_id for rule in report.rules if rule.operational}
        assert "eta_delay" in operationnelles
        assert "immobilization_anomaly" in operationnelles

    def test_demande_a_lediteur_formulee_en_besoin_metier(self, fleetlink, shipped_rules):
        """L'éditeur sait ce qu'est « un écart à l'itinéraire », pas `get_deviation`."""
        demands = build_report(fleetlink, shipped_rules).required_from_vendor()

        assert len(demands) == 1
        assert "écart à l'itinéraire" in demands[0]
        assert "deviation_persistent" in demands[0]  # règle concernée citée

    def test_rapport_lisible(self, fleetlink, shipped_rules):
        rendu = build_report(fleetlink, shipped_rules).render()
        for expected in (
            "FleetLink",
            "dégradé",
            "Détecter un écart à l'itinéraire",
            "ne déclencheront jamais",
            "deviation_persistent",
            "À demander à l'éditeur du TMS",
        ):
            assert expected in rendu


class TestTmsInexploitable:
    def test_sans_liste_de_missions_le_tms_est_bloque(self, x_tms, shipped_rules):
        """Rien à surveiller : le raccordement doit être refusé, pas dégradé."""
        crippled = x_tms.model_copy(
            update={
                "operations": {
                    **x_tms.operations,
                    Operation.LIST_MISSIONS: x_tms.operations[Operation.LIST_MISSIONS].model_copy(
                        update={"path": None, "unsupported": True}
                    ),
                }
            }
        )
        report = build_report(crippled, shipped_rules)
        assert report.verdict is Verdict.BLOCKED


class TestAnalyseStatique:
    def test_une_regle_sans_dependance_reste_operationnelle(self, fleetlink, shipped_rules):
        """L'analyse ne prétend pas qu'une règle va déclencher, seulement qu'elle peut."""
        incident = shipped_rules.by_id("critical_incident")
        status = rule_status(fleetlink, incident)
        assert status.operational is True

    def test_les_champs_derives_ne_comptent_pas_comme_manquants(self, fleetlink, shipped_rules):
        """`event.distance_to_target_km` est calculé par nous, pas lu du TMS."""
        zone = shipped_rules.by_id("arrival_out_of_zone")
        status = rule_status(fleetlink, zone)
        assert status.operational is True
