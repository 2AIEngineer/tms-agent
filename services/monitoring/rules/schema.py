"""Schéma d'une règle métier, validé strictement au chargement.

Ce fichier est le contrat passé à l'équipe métier : tout ce qu'elle peut écrire
dans un YAML, et rien de plus. `extra="forbid"` partout est délibéré — une clé
mal orthographiée doit faire échouer le chargement, pas être ignorée en
silence, sinon une règle paraît active alors qu'elle ne surveille rien.
"""

from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.mapped_dto import NotificationChannel, RuleAction, Severity

from .operators import InvalidRuleValue
from .operators import get as get_operator

# Préfixe qui fait d'une règle une règle d'événement plutôt qu'une règle d'état.
EVENT_FIELD_PREFIX = "event."


class RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Condition(RuleModel):
    """Condition élémentaire : un chemin, un opérateur, une valeur attendue."""

    field: str = Field(min_length=1, description="Chemin dans le contexte, ex. 'deviation.duration_minutes'.")
    operator: str
    value: Any = None

    @model_validator(mode="after")
    def _check_operator_and_value(self) -> Self:
        """Vérifie l'opérateur et le type de sa valeur dès le chargement.

        C'est ici que se joue la promesse faite au métier : une règle au seuil
        absurde ou à l'opérateur inexistant est refusée au dépôt du fichier.
        """
        operator = get_operator(self.operator)
        try:
            operator.validate(self.value)
        except InvalidRuleValue as error:
            raise ValueError(f"champ '{self.field}' : {error}") from error
        return self


class AllOf(RuleModel):
    """Conjonction : toutes les sous-conditions doivent être vraies."""

    all_: list["ConditionNode"] = Field(alias="all", min_length=1)


class AnyOf(RuleModel):
    """Disjonction : au moins une sous-condition doit être vraie."""

    any_: list["ConditionNode"] = Field(alias="any", min_length=1)


class NotOf(RuleModel):
    """Négation d'une sous-condition."""

    not_: "ConditionNode" = Field(alias="not")


# Un nœud est soit un groupe logique, soit une condition élémentaire. Les
# groupes passent avant pour que `{all: [...]}` ne soit pas confondu avec une
# condition dont les clés manqueraient.
ConditionNode = Annotated[AllOf | AnyOf | NotOf | Condition, Field(union_mode="left_to_right")]


class AlertTemplate(RuleModel):
    """Gabarit de l'alerte produite par la règle."""

    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    deduplication_key: str = Field(
        min_length=1,
        description="Gabarit de la clé d'unicité d'une alerte ouverte, ex. 'deviation:{{ mission.id }}'.",
    )
    cooldown_minutes: int = Field(default=0, ge=0)
    auto_resolve: bool = Field(
        default=True,
        description="Clôture l'alerte quand la règle cesse d'être vérifiée (F13). "
        "Sans effet sur une règle d'événement, qu'on ne peut pas réévaluer.",
    )


class NotifySpec(RuleModel):
    """Destinataire d'une notification, désigné par rôle ou par identifiant."""

    role: str | None = None
    user_id: str | None = None
    management_level: str = Field(pattern=r"^M\+\d+$")
    channel: NotificationChannel

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if bool(self.role) == bool(self.user_id):
            raise ValueError("renseigner soit 'role', soit 'user_id', mais pas les deux")
        return self


class ThenBlock(RuleModel):
    """Ce que la règle produit lorsqu'elle se déclenche."""

    action: RuleAction
    alert: AlertTemplate | None = None
    notify: list[NotifySpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _action_consistency(self) -> Self:
        if self.action is RuleAction.EMIT_ALERT and self.alert is None:
            raise ValueError("l'action 'emit_alert' exige un bloc 'alert'")
        # F12 : log_only et create_report sont journalisées sans alerte ni
        # notification. Autoriser un bloc `notify` ici laisserait croire à son
        # auteur que quelqu'un sera prévenu.
        if self.action is not RuleAction.EMIT_ALERT and self.notify:
            raise ValueError(f"l'action '{self.action.value}' ne notifie personne : retirer le bloc 'notify'")
        return self


class Rule(RuleModel):
    """Une règle métier complète, telle qu'un fichier YAML la décrit."""

    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    version: int = Field(ge=1)
    enabled: bool = True
    severity: Severity
    category: str = Field(min_length=1)
    author: str | None = None
    description: str | None = None

    when: ConditionNode
    observe: list[str] = Field(
        default_factory=list,
        description="Chemins supplémentaires recopiés dans l'alerte comme preuves (F10).",
    )
    then: ThenBlock

    @property
    def is_event_rule(self) -> bool:
        """Vrai si la règle porte sur un événement plutôt que sur un état.

        Déduit des chemins employés, et non déclaré par l'auteur : une règle qui
        lit `event.*` ne peut être évaluée qu'à l'arrivée d'un événement, et une
        seule fois (F7). Laisser ce choix à une clé YAML ouvrirait la porte à
        une règle d'événement réévaluée à chaque cycle.
        """
        return any(path.startswith(EVENT_FIELD_PREFIX) for path in self.referenced_fields)

    @property
    def referenced_fields(self) -> tuple[str, ...]:
        """Tous les chemins lus par la règle, conditions et `observe` compris."""
        return tuple(_collect_fields(self.when)) + tuple(self.observe)


def _collect_fields(node: ConditionNode) -> list[str]:
    if isinstance(node, Condition):
        return [node.field]
    if isinstance(node, AllOf):
        return [path for child in node.all_ for path in _collect_fields(child)]
    if isinstance(node, AnyOf):
        return [path for child in node.any_ for path in _collect_fields(child)]
    return _collect_fields(node.not_)


class RuleFile(RuleModel):
    """Enveloppe d'un fichier de règle, utile pour les messages d'erreur."""

    path: str
    rule: Rule
