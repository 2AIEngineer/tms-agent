"""Contrats de l'enquête IA.

L'enquête est la seule partie du système où un LLM décide de quelque chose. Ces
contrats sont ce qui l'encadre, dans les deux sens :

  - **en entrée**, `InvestigationRequest` donne à l'agent un contexte
    *contrôlé*. Il ne reçoit pas d'accès aux tables : il reçoit l'alerte, les
    notifications déjà parties et des faits calculés de façon déterministe
    (F21). Tout le reste, il doit aller le chercher par les outils MCP ;
  - **en sortie**, `InvestigationResult` est une structure, pas un texte libre.
    C'est ce qui rend l'analyse exploitable par l'interface, les rapports et
    l'audit — et vérifiable par un humain.

Rien ici ne décide d'une alerte. Les règles décident, le LLM explique : une
enquête qui échoue laisse l'alerte intacte, seule l'analyse manque.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .alert import Alert, Severity


class RiskLevel(StrEnum):
    """Risque estimé par l'enquêteur.

    Distinct de la sévérité de l'alerte, qui vient de la règle : une dérive
    `high` peut présenter un risque faible si le chauffeur explique un
    détour autorisé. La règle qualifie la situation, l'enquête la pèse.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class InvestigationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # LLM indisponible, quota épuisé, aucune clé : l'alerte reste valable, seule
    # l'analyse est sautée. C'est un état normal, pas une erreur du système.
    SKIPPED = "skipped"


class EvidenceKind(StrEnum):
    SNAPSHOT = "snapshot"
    EVENT = "event"
    POSITION = "position"
    RULE_EVALUATION = "rule_evaluation"
    NOTIFICATION = "notification"
    DRIVER_STATEMENT = "driver_statement"
    HISTORY = "history"


class Evidence(BaseModel):
    """Un fait sur lequel l'enquête s'appuie.

    Chaque conclusion doit pouvoir être rattachée à une observation. Sans cela,
    une analyse de LLM n'est qu'une opinion, et un opérateur n'a aucun moyen de
    la contredire.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    summary: str = Field(min_length=1, description="Ce que ce fait établit, en une phrase.")
    source: str = Field(description="D'où il vient : outil MCP employé, règle évaluée, événement cité.")
    observed_at: datetime | None = Field(default=None, description="Heure TMS du fait, lorsqu'il en a une.")


class DriverCallAssessment(BaseModel):
    """Ce que l'appel au chauffeur a appris, s'il a eu lieu."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted: bool
    reached: bool = False
    statement: str | None = Field(default=None, description="Déclaration du chauffeur.")
    failure_reason: str | None = None


class RecommendedAction(BaseModel):
    """Une action proposée à l'opérateur.

    L'enquêteur recommande, il n'exécute pas : il n'émet jamais d'alerte ni de
    notification (F26). La décision reste humaine, et le bénéfice recherché est
    de lui faire gagner du temps, pas de le remplacer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(min_length=1, description="Action à mener.")
    rationale: str | None = Field(default=None, description="Pourquoi.")
    urgency: RiskLevel = RiskLevel.MEDIUM


class InvestigationRequest(BaseModel):
    """Contexte initial remis à l'enquêteur.

    `observed_facts` est calculé de façon **déterministe** avant tout appel au
    LLM (F21). Deux raisons : lui épargner des lectures pour des faits que nous
    savons calculer, et surtout fournir un socle que son raisonnement ne peut
    pas inventer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: uuid.UUID
    alert_id: uuid.UUID
    tms_id: str
    mission_id: str

    alert: Alert
    observed_facts: dict[str, object] = Field(default_factory=dict)
    # Qui a déjà été prévenu : change les recommandations de l'enquêteur, qui
    # n'a pas à proposer d'alerter un superviseur déjà joint.
    notifications_sent: tuple[str, ...] = ()

    requested_at: datetime


class InvestigationResult(BaseModel):
    """Évaluation structurée jointe à l'alerte (F22).

    Rendue par l'outil `submit_assessment`, en décodage contraint : le LLM ne
    peut pas rendre autre chose que cette forme. Un rapport en texte libre
    serait inexploitable par l'interface et invérifiable à l'audit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: uuid.UUID
    alert_id: uuid.UUID

    summary: str = Field(min_length=1, description="Une phrase, lisible par un opérateur pressé.")
    situation: str = Field(min_length=1, description="Ce qui se passe, factuellement.")
    probable_cause: str | None = Field(
        default=None,
        description="Cause la plus probable, ou absence de cause identifiée. "
        "Ne jamais inventer une cause pour remplir le champ.",
    )

    risk: RiskLevel
    client_impact: str | None = None

    recommended_actions: tuple[RecommendedAction, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    driver_call: DriverCallAssessment | None = None

    false_positive_suspicion: bool = Field(
        default=False,
        description="L'enquêteur estime que la règle a déclenché à tort. Signalé et non "
        "appliqué : seul l'opérateur clôt une alerte, et c'est un signal pour "
        "faire évoluer la règle.",
    )
    confidence: float = Field(ge=0.0, le=1.0)

    completed_at: datetime

    @property
    def actionable(self) -> bool:
        """L'enquête apporte-t-elle quelque chose à décider ?

        Une analyse sans action recommandée et peu assurée vaut d'être signalée
        comme telle plutôt que présentée comme une conclusion.
        """
        return bool(self.recommended_actions) and self.confidence >= 0.5


def minimum_severity_for_investigation(severity: Severity, threshold: Severity) -> bool:
    """La sévérité justifie-t-elle une enquête (F19) ?

    Fonction nommée plutôt que comparaison écrite partout : le seuil est une
    décision d'exploitation, et la relire au même endroit évite qu'une des
    comparaisons dérive.
    """
    return severity >= threshold
