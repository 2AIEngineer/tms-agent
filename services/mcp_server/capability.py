"""Rapport de capacité d'un TMS : ce qu'il sait fournir, et ce qui en découle.

Quand un TMS ne sait pas rendre une information, deux issues sont possibles.
La mauvaise : les règles qui en dépendent ne déclenchent jamais, sans que
personne ne le sache — et l'absence d'alerte ressemble en tout point à une
flotte sans anomalie. La bonne : le manque est constaté à la connexion, et la
conséquence écrite noir sur blanc.

Ce module produit la seconde. Le rapport répond à trois questions :

  1. quelles capacités ce TMS offre-t-il ?
  2. quelles règles métier en deviennent inopérantes ?
  3. que faut-il demander à l'éditeur du TMS pour y remédier ?

C'est le document à mettre sur la table lors d'un raccordement.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from services.monitoring.rules.loader import RuleSet
from services.monitoring.rules.schema import Rule

from .connectors.dialect import Operation, TmsDialect

# Préfixe de contexte de règle -> opération qui l'alimente.
#
# C'est le lien entre ce que les règles lisent et ce que le TMS doit savoir
# rendre. Sans `get_deviation`, le contexte n'a pas de clé `deviation`, et
# toute condition portant sur `deviation.*` est définitivement fausse.
CONTEXT_REQUIREMENTS: dict[str, Operation] = {
    "mission": Operation.GET_MISSION,
    "vehicle": Operation.GET_VEHICLE,
    "driver": Operation.GET_DRIVER,
    "deviation": Operation.GET_DEVIATION,
    "eta": Operation.GET_ETA,
    "route": Operation.GET_ROUTE,
    "event": Operation.GET_MISSION,
}

# Capacités fonctionnelles, exprimées dans les termes de la spécification
# plutôt que dans ceux du code : c'est ce qu'un interlocuteur métier lit.
FEATURES: dict[str, tuple[str, tuple[Operation, ...]]] = {
    "surveillance": ("Surveiller les missions en cours", (Operation.LIST_MISSIONS, Operation.GET_MISSION)),
    "heure_tms": ("Mesurer retards et âges sur l'horloge du TMS (F3)", (Operation.HEALTH,)),
    "derive": ("Détecter un écart à l'itinéraire", (Operation.GET_DEVIATION,)),
    "retard": ("Détecter un retard de livraison", (Operation.GET_ETA,)),
    "immobilisation": ("Détecter une immobilisation anormale", (Operation.GET_VEHICLE,)),
    "position": ("Situer le véhicule et reconstituer son trajet", (Operation.GET_VEHICLE,)),
    "itineraire": ("Comparer la position à l'itinéraire planifié", (Operation.GET_ROUTE,)),
    "appel_chauffeur": ("Joindre le chauffeur de la mission (F16, M+0)", (Operation.GET_DRIVER,)),
    "escalade": ("Résoudre les destinataires par rôle (F16, M+1 à M+4)", (Operation.LIST_USERS_BY_ROLE,)),
}


class Verdict(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    key: str
    label: str
    available: bool
    missing_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuleStatus:
    """Sort d'une règle métier face aux capacités de ce TMS."""

    rule_id: str
    severity: str
    operational: bool
    # Chemins que la règle lit et que ce TMS ne peut pas alimenter.
    unavailable_fields: tuple[str, ...]
    missing_operations: tuple[str, ...]


@dataclass(slots=True)
class CapabilityReport:
    tms_id: str
    label: str
    features: list[FeatureStatus] = field(default_factory=list)
    rules: list[RuleStatus] = field(default_factory=list)
    unsupported_operations: tuple[str, ...] = ()

    @property
    def inoperative_rules(self) -> list[RuleStatus]:
        return [rule for rule in self.rules if not rule.operational]

    @property
    def verdict(self) -> Verdict:
        """Un TMS est bloqué si plus rien ne peut être surveillé."""
        surveillance = next((f for f in self.features if f.key == "surveillance"), None)
        if surveillance is not None and not surveillance.available:
            return Verdict.BLOCKED
        if self.inoperative_rules or any(not f.available for f in self.features):
            return Verdict.DEGRADED
        return Verdict.OK

    def required_from_vendor(self) -> list[str]:
        """Ce qu'il faut demander à l'éditeur du TMS, formulé pour lui.

        Exprimé en besoin métier et non en nom d'endpoint : l'éditeur sait quelle
        ressource chez lui porte « l'écart à l'itinéraire », pas ce que notre
        connecteur appelle `get_deviation`.
        """
        demands: list[str] = []
        for status in self.features:
            if status.available:
                continue
            concerned = sorted(
                rule.rule_id
                for rule in self.inoperative_rules
                if set(rule.missing_operations) & set(status.missing_operations)
            )
            suffix = f" — règles concernées : {', '.join(concerned)}" if concerned else ""
            demands.append(f"{status.label}{suffix}")
        return demands

    def render(self) -> str:
        """Rapport lisible, destiné à un raccordement de TMS."""
        marks = {Verdict.OK: "complet", Verdict.DEGRADED: "dégradé", Verdict.BLOCKED: "inexploitable"}
        lines = [
            f"Rapport de capacité — {self.label} ({self.tms_id})",
            f"Verdict : {marks[self.verdict]}",
            "",
            "Capacités :",
        ]
        for status in self.features:
            mark = "oui" if status.available else "NON"
            detail = "" if status.available else f"  (manque : {', '.join(status.missing_operations)})"
            lines.append(f"  [{mark:>3}] {status.label}{detail}")

        inoperative = self.inoperative_rules
        lines.append("")
        if inoperative:
            lines.append("Règles qui ne déclencheront jamais sur ce TMS :")
            for rule in inoperative:
                lines.append(
                    f"  - {rule.rule_id} ({rule.severity}) : "
                    f"champs indisponibles {', '.join(rule.unavailable_fields)}"
                )
        else:
            lines.append("Toutes les règles livrées sont opérationnelles sur ce TMS.")

        demands = self.required_from_vendor()
        if demands:
            lines.extend(["", "À demander à l'éditeur du TMS :"])
            lines.extend(f"  - {demand}" for demand in demands)
        return "\n".join(lines)


def _missing_for(dialect: TmsDialect, operations: tuple[Operation, ...]) -> tuple[str, ...]:
    return tuple(operation.value for operation in operations if not dialect.supports(operation))


def rule_status(dialect: TmsDialect, rule: Rule) -> RuleStatus:
    """Détermine si une règle peut déclencher sur ce TMS.

    L'analyse est statique : elle croise les chemins que la règle lit avec les
    opérations que le dialecte déclare. Elle ne dit pas qu'une règle *va*
    déclencher — cela dépend des données — mais elle dit à coup sûr quand une
    règle ne *peut pas* déclencher, ce qui est l'information utile.
    """
    unavailable: list[str] = []
    missing: set[str] = set()

    for path in rule.referenced_fields:
        prefix = path.split(".", 1)[0]
        operation = CONTEXT_REQUIREMENTS.get(prefix)
        if operation is None:
            continue
        if not dialect.supports(operation):
            unavailable.append(path)
            missing.add(operation.value)

    return RuleStatus(
        rule_id=rule.id,
        severity=rule.severity.value,
        operational=not unavailable,
        unavailable_fields=tuple(sorted(set(unavailable))),
        missing_operations=tuple(sorted(missing)),
    )


def build_report(dialect: TmsDialect, ruleset: RuleSet | None = None) -> CapabilityReport:
    """Construit le rapport de capacité d'un TMS à partir de son dialecte."""
    features = [
        FeatureStatus(
            key=key,
            label=label,
            available=not _missing_for(dialect, operations),
            missing_operations=_missing_for(dialect, operations),
        )
        for key, (label, operations) in FEATURES.items()
    ]

    rules = [rule_status(dialect, rule) for rule in (ruleset.rules if ruleset else ())]

    return CapabilityReport(
        tms_id=dialect.tms_id,
        label=dialect.label,
        features=features,
        rules=rules,
        unsupported_operations=tuple(
            sorted(operation.value for operation in Operation if not dialect.supports(operation))
        ),
    )
