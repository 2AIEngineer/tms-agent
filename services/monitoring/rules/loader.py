"""Chargement et rechargement à chaud des règles YAML.

Deux garanties, qui sont la raison d'être de ce module :

  - un fichier invalide ne remplace jamais le jeu de règles en vigueur. Sans
    cela, une faute de frappe de l'équipe métier désarmerait la surveillance
    sans que personne ne s'en aperçoive (F8) ;
  - le rechargement se déclenche sur changement de fichier, sans redémarrage,
    pour qu'une modification de seuil soit active au cycle suivant.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .schema import Rule

logger = logging.getLogger(__name__)

RULE_FILE_PATTERNS = ("*.yaml", "*.yml")


@dataclass(frozen=True, slots=True)
class RuleLoadError:
    """Un fichier refusé, avec de quoi le corriger."""

    path: Path
    message: str

    def __str__(self) -> str:
        return f"{self.path.name} : {self.message}"


@dataclass(frozen=True, slots=True)
class RuleSet:
    """Jeu de règles chargé, avec les fichiers qui ont été refusés.

    Les erreurs voyagent avec les règles au lieu d'être levées : un cycle doit
    pouvoir tourner sur les règles valides tout en signalant les autres.
    """

    rules: tuple[Rule, ...] = ()
    errors: tuple[RuleLoadError, ...] = ()
    fingerprint: tuple[tuple[str, float], ...] = field(default=(), repr=False)

    @property
    def state_rules(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled and not rule.is_event_rule)

    @property
    def event_rules(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.enabled and rule.is_event_rule)

    def by_id(self, rule_id: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)


def _fingerprint(directory: Path) -> tuple[tuple[str, float], ...]:
    """Empreinte du répertoire : (nom, date de modification) par fichier.

    Suffisant et peu coûteux pour un rechargement à chaud appelé à chaque
    cycle : inutile de lire le contenu de tous les fichiers pour savoir
    qu'aucun n'a changé.
    """
    return tuple(sorted((path.name, path.stat().st_mtime) for path in _rule_files(directory)))


def _rule_files(directory: Path) -> list[Path]:
    return sorted(path for pattern in RULE_FILE_PATTERNS for path in directory.glob(pattern))


def _parse(path: Path) -> tuple[Rule | None, RuleLoadError | None]:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return None, RuleLoadError(path, f"YAML illisible — {error}")

    if not isinstance(raw, dict):
        return None, RuleLoadError(path, "le fichier doit contenir un mapping décrivant une règle")

    try:
        return Rule.model_validate(raw), None
    except ValidationError as error:
        return None, RuleLoadError(path, _format_validation_error(error))


def _format_validation_error(error: ValidationError) -> str:
    """Message orienté auteur de règle, pas trace Pydantic.

    L'équipe métier doit pouvoir corriger seule : on lui donne le chemin de la
    clé fautive et la raison, pas le détail des unions tentées.
    """
    parts = []
    for item in error.errors():
        location = ".".join(str(piece) for piece in item["loc"]) or "(racine)"
        parts.append(f"{location} → {item['msg']}")
    return " ; ".join(dict.fromkeys(parts))


def load_rules(directory: Path) -> RuleSet:
    """Charge tous les fichiers de règles d'un répertoire.

    Un identifiant en doublon est refusé : deux règles de même `id` rendraient
    la déduplication et l'audit ambigus.
    """
    if not directory.is_dir():
        return RuleSet(errors=(RuleLoadError(directory, "répertoire de règles introuvable"),))

    rules: list[Rule] = []
    errors: list[RuleLoadError] = []
    seen: dict[str, Path] = {}

    for path in _rule_files(directory):
        rule, error = _parse(path)
        if error is not None or rule is None:
            errors.append(error or RuleLoadError(path, "règle illisible"))
            continue
        if rule.id in seen:
            duplicate = f"identifiant '{rule.id}' déjà défini par {seen[rule.id].name}"
            errors.append(RuleLoadError(path, duplicate))
            continue
        seen[rule.id] = path
        rules.append(rule)

    return RuleSet(
        rules=tuple(sorted(rules, key=lambda rule: rule.id)),
        errors=tuple(errors),
        fingerprint=_fingerprint(directory),
    )


class RuleRepository:
    """Détient le jeu de règles en vigueur et le recharge quand les fichiers changent.

    À interroger au début de chaque cycle. Si le rechargement ne produit
    aucune règle valide alors que le jeu courant en contenait, l'ancien jeu est
    conservé : mieux vaut surveiller avec des règles d'hier que ne plus
    surveiller du tout.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._ruleset = load_rules(self._directory)
        self._report(self._ruleset, initial=True)

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def current(self) -> RuleSet:
        return self._ruleset

    def reload_if_changed(self) -> RuleSet:
        """Recharge si une date de modification a bougé, puis rend le jeu actif."""
        if not self._directory.is_dir():
            logger.error("Répertoire de règles introuvable : %s — jeu de règles conservé", self._directory)
            return self._ruleset

        if _fingerprint(self._directory) == self._ruleset.fingerprint:
            return self._ruleset

        candidate = load_rules(self._directory)

        if not candidate.rules and self._ruleset.rules:
            logger.error(
                "Rechargement refusé : aucune règle valide dans %s (%s). Jeu précédent conservé (%d règles).",
                self._directory,
                "; ".join(str(error) for error in candidate.errors),
                len(self._ruleset.rules),
            )
            # L'empreinte est mise à jour malgré le refus, pour ne pas réessayer
            # le même jeu fautif à chaque cycle et noyer les journaux.
            self._ruleset = RuleSet(
                rules=self._ruleset.rules,
                errors=candidate.errors,
                fingerprint=candidate.fingerprint,
            )
            return self._ruleset

        self._ruleset = candidate
        self._report(candidate, initial=False)
        return self._ruleset

    @staticmethod
    def _report(ruleset: RuleSet, *, initial: bool) -> None:
        verb = "Chargement" if initial else "Rechargement"
        logger.info(
            "%s des règles : %d active(s) — %d d'état, %d d'événement",
            verb,
            len(ruleset.rules),
            len(ruleset.state_rules),
            len(ruleset.event_rules),
        )
        for error in ruleset.errors:
            logger.error("Règle refusée — %s", error)
