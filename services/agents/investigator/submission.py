"""Ce que le modèle est autorisé à rendre : l'outil `submit_assessment`.

Le schéma de cet outil est **dérivé du contrat pivot**, pas écrit à la main : le
modèle ne peut donc rendre qu'une forme déjà validée, et une évolution du
contrat se propage sans risque d'oubli.

Un point de correction : **le modèle ne renseigne aucun identifiant.** Ni
`investigation_id`, ni `alert_id`, ni `completed_at`. Les lui confier
reviendrait à lui permettre d'attacher son analyse à une autre alerte que celle
qu'il a examinée — une erreur silencieuse et grave, puisque l'opérateur lirait
une explication qui ne correspond pas à son alerte. Le service les remplit.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.mapped_dto import (
    DriverCallAssessment,
    Evidence,
    InvestigationResult,
    RecommendedAction,
    RiskLevel,
)

SUBMIT_TOOL_NAME = "submit_assessment"


class InvestigationSubmission(BaseModel):
    """Évaluation telle que le modèle la rend.

    Mêmes champs que `InvestigationResult`, **moins les identifiants et les
    horodatages** : tout ce que le service sait déjà et que le modèle n'a pas à
    inventer.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, description="Une phrase pour un opérateur pressé.")
    situation: str = Field(min_length=1, description="Les faits établis, sans interprétation.")
    probable_cause: str | None = Field(
        default=None, description="Cause probable, ou vide si aucune ne se dégage."
    )
    risk: RiskLevel = Field(description="Risque réel estimé.")
    client_impact: str | None = Field(default=None, description="Conséquence pour le client.")
    recommended_actions: list[RecommendedAction] = Field(
        default_factory=list, description="Actions proposées à l'opérateur."
    )
    evidence: list[Evidence] = Field(
        default_factory=list, description="Faits appuyant chaque conclusion, avec leur source."
    )
    driver_call: DriverCallAssessment | None = Field(
        default=None, description="Ce qu'un appel au chauffeur a appris, s'il a eu lieu."
    )
    false_positive_suspicion: bool = Field(
        default=False, description="Vrai si la règle semble avoir déclenché à tort."
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Assurance de l'analyse, de 0 à 1.")

    def to_result(
        self,
        *,
        investigation_id: uuid.UUID,
        alert_id: uuid.UUID,
        completed_at: datetime | None = None,
    ) -> InvestigationResult:
        """Complète la soumission avec ce que le service seul connaît."""
        return InvestigationResult(
            investigation_id=investigation_id,
            alert_id=alert_id,
            summary=self.summary,
            situation=self.situation,
            probable_cause=self.probable_cause,
            risk=self.risk,
            client_impact=self.client_impact,
            recommended_actions=tuple(self.recommended_actions),
            evidence=tuple(self.evidence),
            driver_call=self.driver_call,
            false_positive_suspicion=self.false_positive_suspicion,
            confidence=self.confidence,
            completed_at=completed_at or datetime.now(UTC),
        )


# Profondeur maximale de déréférencement. Nos modèles ne sont pas récursifs ;
# ce plafond n'est qu'un garde-fou contre une référence circulaire.
MAX_SCHEMA_DEPTH = 12


def inline_refs(schema: Any, definitions: dict[str, Any], depth: int = 0) -> Any:
    """Remplace les `$ref` par leur définition, récursivement.

    **Le schéma doit être plat.** Mesuré sur le modèle local — Qwen 7B servi par
    vLLM en décodage contraint — un schéma imbriqué par `$defs` et `$ref` ne
    produit aucun appel d'outil : le modèle répond en prose malgré un
    `tool_choice` imposé. Le même schéma aplati fonctionne du premier coup.

    Ce n'est pas propre à ce modèle : plusieurs fournisseurs restreignent les
    références dans leurs modes contraints. Aplatir rend le schéma portable, et
    reste mécanique — il demeure dérivé du contrat pivot.
    """
    if depth > MAX_SCHEMA_DEPTH:
        return {"type": "object"}
    if isinstance(schema, dict):
        reference = schema.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = definitions.get(reference.removeprefix("#/$defs/"), {})
            return inline_refs(target, definitions, depth + 1)
        return {
            key: inline_refs(value, definitions, depth + 1) for key, value in schema.items() if key != "$defs"
        }
    if isinstance(schema, list):
        return [inline_refs(item, definitions, depth + 1) for item in schema]
    return schema


def compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Prépare le schéma engendré pour le décodage contraint d'un modèle.

    Deux transformations, toutes deux mécaniques :

      - **aplatir les références** (voir `inline_refs`), sans quoi le modèle
        local ne produit aucun appel d'outil ;
      - **retirer `title` et `default`**. Pydantic en engendre un par propriété
        et par sous-modèle ; ils servent aux interfaces de documentation et, pour
        le modèle, ne répètent que le nom du champ tout en occupant la fenêtre
        de contexte à chaque tour d'enquête.

    Le schéma reste dérivé du contrat : une évolution de celui-ci s'y propage
    sans intervention.
    """
    flattened = inline_refs(schema, schema.get("$defs", {}))
    stripped: dict[str, Any] = _strip(flattened)
    return stripped


def _strip(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {key: _strip(value) for key, value in schema.items() if key not in ("title", "default")}
    if isinstance(schema, list):
        return [_strip(item) for item in schema]
    return schema


def submit_tool_definition() -> dict[str, Any]:
    """Définition de l'outil de conclusion, au format d'appel d'outils.

    Le schéma est engendré depuis le modèle Pydantic : un champ ajouté au
    contrat devient automatiquement un champ attendu du modèle, sans risque
    d'oublier de mettre le schéma à jour.

    Les descriptions sont **courtes à dessein**. Ce schéma est renvoyé au modèle
    à chaque tour d'enquête : mesuré à 1641 jetons dans une première version
    bavarde, il pesait plus que les six outils MCP réunis. Les règles de
    raisonnement appartiennent au prompt système, où elles sont énoncées une
    fois ; le schéma ne garde que ce qui est nécessaire pour remplir un champ.
    """
    schema = compact_schema(InvestigationSubmission.model_json_schema())
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_TOOL_NAME,
            "description": "Conclut l'enquête. À appeler exactement une fois, en dernier.",
            "parameters": schema,
        },
    }
