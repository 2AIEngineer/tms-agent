"""Résultats du cycle de vie des alertes.

Ces objets ne sont pas des contrats inter-services : ils décrivent ce qui s'est
passé pendant un cycle, pour le bilan et les journaux. Ils restent donc dans le
service de surveillance.
"""

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from shared.mapped_dto import RuleEvaluation, Severity


class SuppressionReason(StrEnum):
    """Pourquoi un déclenchement n'a pas produit d'alerte.

    La raison est conservée et comptée dans le bilan du cycle (F11) : une
    suppression silencieuse rendrait impossible de distinguer « la règle n'a
    pas déclenché » de « elle a déclenché mais a été étouffée ».
    """

    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"


@dataclass(frozen=True, slots=True)
class AlertCreated:
    alert_id: uuid.UUID
    rule_id: str
    severity: Severity
    deduplication_key: str
    notifications_enqueued: int


@dataclass(frozen=True, slots=True)
class AlertSuppressed:
    rule_id: str
    severity: Severity
    deduplication_key: str
    reason: SuppressionReason


@dataclass(frozen=True, slots=True)
class AlertResolved:
    alert_id: uuid.UUID
    rule_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RuleLogged:
    """Déclenchement d'une règle `log_only` ou `create_report` (F12)."""

    rule_id: str
    action: str


@dataclass(slots=True)
class AlertCycleReport:
    """Bilan des décisions d'alerte pour une mission dans un cycle."""

    tms_id: str
    mission_id: str
    created: list[AlertCreated] = field(default_factory=list)
    suppressed: list[AlertSuppressed] = field(default_factory=list)
    resolved: list[AlertResolved] = field(default_factory=list)
    logged: list[RuleLogged] = field(default_factory=list)

    def merge(self, other: "AlertCycleReport") -> None:
        self.created.extend(other.created)
        self.suppressed.extend(other.suppressed)
        self.resolved.extend(other.resolved)
        self.logged.extend(other.logged)

    @property
    def investigable(self) -> list[AlertCreated]:
        """Alertes créées, les plus graves d'abord.

        Ordre d'attribution des enquêtes au LLM (F19) : avec un quota par
        cycle, l'ordre détermine ce qui sera analysé et ce qui attendra.
        """
        return sorted(self.created, key=lambda created: created.severity.rank, reverse=True)

    def summary(self) -> str:
        return (
            f"tms={self.tms_id} mission={self.mission_id} créées={len(self.created)} "
            f"supprimées={len(self.suppressed)} closes={len(self.resolved)} "
            f"journalisées={len(self.logged)}"
        )


def alert_fingerprint(evaluation: RuleEvaluation) -> str:
    """Clé de déduplication effective d'une évaluation.

    Repli sur `règle:mission` si la règle n'en fournit pas : une clé vide
    ferait collisionner des alertes de missions différentes dans l'index unique.
    """
    return evaluation.deduplication_key or f"{evaluation.rule_id}:{evaluation.mission_id}"
