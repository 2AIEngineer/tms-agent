"""Tests de validation du schéma de règle.

Ce qui est vérifié ici, c'est la promesse faite à l'équipe métier : une règle
mal écrite est refusée au dépôt du fichier, avec un message exploitable, et
jamais acceptée à moitié.
"""

import re

import pytest
import yaml
from pydantic import ValidationError

from services.monitoring.rules.schema import AllOf, AnyOf, Condition, NotOf, Rule

REGLE_MINIMALE = """
id: regle_test
version: 1
severity: high
category: route_deviation
when:
  field: deviation.current_offset_km
  operator: greater_than
  value: 5
then:
  action: emit_alert
  alert:
    title: "Dérive"
    message: "Écart de {{ deviation.current_offset_km }} km"
    deduplication_key: "deviation:{{ mission.id }}"
"""


def build(yaml_text: str) -> Rule:
    return Rule.model_validate(yaml.safe_load(yaml_text))


def test_regle_minimale_valide():
    rule = build(REGLE_MINIMALE)
    assert rule.id == "regle_test"
    assert rule.enabled is True  # activée par défaut
    assert rule.then.alert is not None
    assert rule.then.alert.auto_resolve is True  # F13 : clôture auto par défaut


class TestStructureLogique:
    def test_all_any_not_imbricables(self):
        rule = build("""
id: imbriquee
version: 1
severity: low
category: delay
when:
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - any:
        - field: eta.delay_minutes
          operator: greater_than
          value: 45
        - not:
            field: vehicle.status
            operator: equals
            value: moving
then:
  action: log_only
""")
        assert isinstance(rule.when, AllOf)
        assert isinstance(rule.when.all_[1], AnyOf)
        assert isinstance(rule.when.all_[1].any_[1], NotOf)
        assert isinstance(rule.when.all_[1].any_[1].not_, Condition)

    def test_groupe_vide_refuse(self):
        """`all: []` serait vrai pour toute mission : l'alerte partirait en boucle."""
        with pytest.raises(ValidationError):
            build("""
id: vide
version: 1
severity: low
category: delay
when:
  all: []
then:
  action: log_only
""")


class TestRefusDesReglesIncorrectes:
    def test_operateur_inexistant(self):
        with pytest.raises(ValidationError, match="Opérateur inconnu"):
            build(REGLE_MINIMALE.replace("operator: greater_than", "operator: plus_grand"))

    def test_seuil_du_mauvais_type(self):
        with pytest.raises(ValidationError, match="attend un nombre"):
            build(REGLE_MINIMALE.replace("value: 5", 'value: "cinq km"'))

    def test_message_derreur_cite_le_champ_fautif(self):
        """L'auteur de la règle doit savoir quelle ligne corriger."""
        with pytest.raises(ValidationError, match=re.escape("deviation.current_offset_km")):
            build(REGLE_MINIMALE.replace("value: 5", 'value: "cinq km"'))

    def test_emit_alert_sans_bloc_alert(self):
        with pytest.raises(ValidationError, match="exige un bloc 'alert'"):
            build("""
id: sans_alerte
version: 1
severity: high
category: delay
when:
  field: eta.delay_minutes
  operator: greater_than
  value: 45
then:
  action: emit_alert
""")

    def test_log_only_avec_notify(self):
        """F12 : log_only ne notifie personne. Laisser passer un bloc `notify`
        tromperait son auteur, qui croirait quelqu'un prévenu."""
        with pytest.raises(ValidationError, match="ne notifie personne"):
            build("""
id: log_avec_notify
version: 1
severity: low
category: delay
when:
  field: eta.delay_minutes
  operator: greater_than
  value: 45
then:
  action: log_only
  notify:
    - role: ops_agent
      management_level: M+1
      channel: in_app
""")

    def test_destinataire_ambigu(self):
        with pytest.raises(ValidationError, match="soit 'role', soit 'user_id'"):
            build(
                REGLE_MINIMALE
                + """  notify:
    - role: ops_agent
      user_id: USR-1
      management_level: M+1
      channel: in_app
"""
            )

    def test_destinataire_absent(self):
        with pytest.raises(ValidationError, match="soit 'role', soit 'user_id'"):
            build(
                REGLE_MINIMALE
                + """  notify:
    - management_level: M+1
      channel: in_app
"""
            )

    def test_cle_mal_orthographiee_refusee(self):
        """`sevrity` au lieu de `severity` doit échouer, pas être ignoré en silence."""
        with pytest.raises(ValidationError):
            build(REGLE_MINIMALE + "sevrity: low\n")

    def test_canal_inconnu_refuse(self):
        with pytest.raises(ValidationError):
            build(
                REGLE_MINIMALE
                + """  notify:
    - role: ops_agent
      management_level: M+1
      channel: pigeon
"""
            )

    @pytest.mark.parametrize("bad_id", ["Regle-Test", "règle_test", "regle test", ""])
    def test_identifiant_non_conforme(self, bad_id):
        """L'identifiant sert de clé d'audit et de nom de fichier : jeu de caractères restreint."""
        with pytest.raises(ValidationError):
            build(REGLE_MINIMALE.replace("id: regle_test", f"id: {bad_id!r}"))

    def test_cooldown_negatif_refuse(self):
        with pytest.raises(ValidationError):
            build(REGLE_MINIMALE + "    cooldown_minutes: -10\n")


class TestClassificationEtatEvenement:
    def test_regle_detat(self):
        assert build(REGLE_MINIMALE).is_event_rule is False

    def test_regle_devenement_deduite_du_chemin(self):
        """La nature de la règle est déduite, pas déclarée : une règle lisant
        `event.*` ne peut être évaluée qu'à l'arrivée d'un événement (F7)."""
        rule = build("""
id: incident
version: 1
severity: critical
category: safety
when:
  all:
    - field: mission.status
      operator: equals
      value: in_progress
    - field: event.payload.severity
      operator: equals
      value: critical
then:
  action: emit_alert
  alert:
    title: "Incident"
    message: "{{ event.payload.text }}"
    deduplication_key: "incident:{{ event.id }}"
""")
        assert rule.is_event_rule is True

    def test_champ_devenement_dans_observe_suffit(self):
        rule = build(REGLE_MINIMALE + "observe:\n  - event.position\n")
        assert rule.is_event_rule is True

    def test_champs_references_incluent_conditions_et_observe(self):
        rule = build(REGLE_MINIMALE + "observe:\n  - deviation.vehicle_position\n")
        assert set(rule.referenced_fields) == {
            "deviation.current_offset_km",
            "deviation.vehicle_position",
        }
