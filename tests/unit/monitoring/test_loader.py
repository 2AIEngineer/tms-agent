"""Tests du chargement et du rechargement à chaud des règles.

La propriété critique testée ici : **un fichier invalide ne désarme jamais la
surveillance**. Une faute de frappe de l'équipe métier doit être signalée, pas
silencieusement transformée en absence de détection (F8).
"""

import os
from pathlib import Path

import pytest

from services.monitoring.rules.loader import RuleRepository, load_rules

REGLE_VALIDE = """
id: {rule_id}
version: {version}
severity: {severity}
category: delay
when:
  field: eta.delay_minutes
  operator: greater_than
  value: {seuil}
then:
  action: emit_alert
  alert:
    title: "Retard"
    message: "Retard de {{{{ eta.delay_minutes }}}} minutes"
    deduplication_key: "retard:{{{{ mission.id }}}}"
"""


def write_rule(directory: Path, name: str, *, rule_id=None, version=1, severity="medium", seuil=45):
    path = directory / f"{name}.yaml"
    path.write_text(
        REGLE_VALIDE.format(rule_id=rule_id or name, version=version, severity=severity, seuil=seuil),
        encoding="utf-8",
    )
    return path


def touch_later(path: Path, seconds: int = 10) -> None:
    """Avance la date de modification d'un fichier.

    Nécessaire parce que deux écritures dans la même milliseconde peuvent
    partager une date de modification, et que le rechargement s'appuie sur
    elle. Le besoin est propre au test : en exploitation, les modifications
    sont espacées de bien plus que cela.
    """
    future = path.stat().st_mtime + seconds
    os.utime(path, (future, future))


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "definitions"
    directory.mkdir()
    return directory


class TestChargement:
    def test_repertoire_vide(self, rules_dir):
        ruleset = load_rules(rules_dir)
        assert ruleset.rules == ()
        assert ruleset.errors == ()

    def test_repertoire_inexistant_signale_sans_lever(self, tmp_path):
        ruleset = load_rules(tmp_path / "absent")
        assert ruleset.rules == ()
        assert "introuvable" in str(ruleset.errors[0])

    def test_chargement_trie_par_identifiant(self, rules_dir):
        write_rule(rules_dir, "zeta")
        write_rule(rules_dir, "alpha")
        assert [rule.id for rule in load_rules(rules_dir).rules] == ["alpha", "zeta"]

    def test_extensions_yaml_et_yml(self, rules_dir):
        write_rule(rules_dir, "une")
        (rules_dir / "deux.yml").write_text(
            REGLE_VALIDE.format(rule_id="deux", version=1, severity="low", seuil=10), encoding="utf-8"
        )
        assert len(load_rules(rules_dir).rules) == 2

    def test_separation_etat_evenement(self, rules_dir):
        write_rule(rules_dir, "etat")
        (rules_dir / "evenement.yaml").write_text(
            """
id: evenement
version: 1
severity: critical
category: safety
when:
  field: event.payload.severity
  operator: equals
  value: critical
then:
  action: log_only
""",
            encoding="utf-8",
        )
        ruleset = load_rules(rules_dir)
        assert [r.id for r in ruleset.state_rules] == ["etat"]
        assert [r.id for r in ruleset.event_rules] == ["evenement"]


class TestFichiersRefuses:
    def test_yaml_illisible_isole(self, rules_dir):
        """Les règles valides restent chargées : un fichier cassé n'en annule pas les autres."""
        write_rule(rules_dir, "bonne")
        (rules_dir / "cassee.yaml").write_text("id: cassee\nwhen: [: ::", encoding="utf-8")

        ruleset = load_rules(rules_dir)
        assert [rule.id for rule in ruleset.rules] == ["bonne"]
        assert len(ruleset.errors) == 1
        assert "YAML illisible" in str(ruleset.errors[0])

    def test_regle_invalide_isolee_avec_message_exploitable(self, rules_dir):
        write_rule(rules_dir, "bonne")
        (rules_dir / "mauvaise.yaml").write_text(
            """
id: mauvaise
version: 1
severity: tres_grave
category: delay
when:
  field: eta.delay_minutes
  operator: greater_than
  value: 45
then:
  action: emit_alert
""",
            encoding="utf-8",
        )
        ruleset = load_rules(rules_dir)
        assert [rule.id for rule in ruleset.rules] == ["bonne"]
        assert "severity" in str(ruleset.errors[0])

    def test_fichier_qui_nest_pas_un_mapping(self, rules_dir):
        (rules_dir / "liste.yaml").write_text("- une\n- deux\n", encoding="utf-8")
        assert "mapping" in str(load_rules(rules_dir).errors[0])

    def test_identifiant_en_doublon_refuse(self, rules_dir):
        """Deux règles de même identifiant rendraient la déduplication et l'audit ambigus."""
        write_rule(rules_dir, "a_premier", rule_id="meme_id")
        write_rule(rules_dir, "b_second", rule_id="meme_id")

        ruleset = load_rules(rules_dir)
        assert len(ruleset.rules) == 1
        assert "déjà défini par a_premier.yaml" in str(ruleset.errors[0])


class TestRechargementAChaud:
    def test_aucun_changement_aucun_rechargement(self, rules_dir):
        write_rule(rules_dir, "regle")
        repository = RuleRepository(rules_dir)
        avant = repository.current
        assert repository.reload_if_changed() is avant

    def test_seuil_modifie_actif_au_cycle_suivant(self, rules_dir):
        """Une modification de seuil doit prendre effet sans redémarrage (F8)."""
        path = write_rule(rules_dir, "regle", seuil=45)
        repository = RuleRepository(rules_dir)
        assert repository.current.by_id("regle").when.value == 45

        write_rule(rules_dir, "regle", seuil=20, version=2)
        touch_later(path)

        rechargees = repository.reload_if_changed()
        assert rechargees.by_id("regle").when.value == 20
        assert rechargees.by_id("regle").version == 2

    def test_nouvelle_regle_deposee(self, rules_dir):
        write_rule(rules_dir, "premiere")
        repository = RuleRepository(rules_dir)

        touch_later(write_rule(rules_dir, "seconde"))
        assert len(repository.reload_if_changed().rules) == 2

    def test_regle_invalide_ne_remplace_pas_le_jeu_en_vigueur(self, rules_dir):
        """Le cas qui compte : la surveillance ne doit pas s'arrêter sur une faute de frappe."""
        path = write_rule(rules_dir, "regle", seuil=45)
        repository = RuleRepository(rules_dir)

        path.write_text("id: regle\nwhen: [: ::", encoding="utf-8")
        touch_later(path)

        apres = repository.reload_if_changed()
        assert [rule.id for rule in apres.rules] == ["regle"]
        assert apres.by_id("regle").when.value == 45  # l'ancienne version reste active
        assert apres.errors  # mais l'erreur est signalée

    def test_jeu_fautif_non_reessaye_a_chaque_cycle(self, rules_dir):
        """L'empreinte est mise à jour malgré le refus, pour ne pas noyer les journaux."""
        path = write_rule(rules_dir, "regle")
        repository = RuleRepository(rules_dir)

        path.write_text("cassee: [", encoding="utf-8")
        touch_later(path)
        premier_refus = repository.reload_if_changed()

        assert repository.reload_if_changed() is premier_refus

    def test_suppression_de_toutes_les_regles_conserve_le_jeu(self, rules_dir):
        """Un répertoire vidé par erreur ne doit pas désarmer la détection."""
        path = write_rule(rules_dir, "regle")
        repository = RuleRepository(rules_dir)

        path.unlink()
        apres = repository.reload_if_changed()
        assert [rule.id for rule in apres.rules] == ["regle"]

    def test_desactivation_explicite_respectee(self, rules_dir):
        """`enabled: false` est une décision, pas une erreur : elle est appliquée."""
        path = write_rule(rules_dir, "regle")
        repository = RuleRepository(rules_dir)

        path.write_text(path.read_text() + "enabled: false\n", encoding="utf-8")
        touch_later(path)

        apres = repository.reload_if_changed()
        assert apres.by_id("regle").enabled is False
        assert apres.state_rules == ()

    def test_repertoire_disparu_conserve_le_jeu(self, rules_dir):
        write_rule(rules_dir, "regle")
        repository = RuleRepository(rules_dir)

        for child in rules_dir.iterdir():
            child.unlink()
        rules_dir.rmdir()

        assert [rule.id for rule in repository.reload_if_changed().rules] == ["regle"]


def test_les_regles_livrees_sont_valides():
    """Garde-fou du dépôt : aucune règle livrée ne doit être refusée au chargement."""
    ruleset = load_rules(Path("services/monitoring/rules/definitions"))
    assert not ruleset.errors, [str(error) for error in ruleset.errors]
    assert len(ruleset.rules) == 6
    assert len(ruleset.event_rules) == 2
