"""Moteur d'évaluation déterministe.

Aucun LLM, aucun appel réseau, aucune base de données : le moteur reçoit un
snapshot et rend des `RuleEvaluation`. C'est cette absence de dépendance qui
permet à l'équipe métier de rejouer ses scénarios sans infrastructure, et au
système de continuer à détecter quand le LLM est indisponible.

Il ne crée pas d'alerte : la déduplication, le cooldown et la clôture
appartiennent à l'Alert Service.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from shared.mapped_dto import (
    MissionEvent,
    MissionSnapshot,
    NotificationRequest,
    RuleEvaluation,
)

from .context import build_event_context, build_state_context
from .operators import MissingType
from .operators import get as get_operator
from .paths import resolve
from .schema import AllOf, AnyOf, Condition, ConditionNode, Rule
from .template import render


def evaluate_condition(node: ConditionNode, context: dict[str, Any], now: datetime) -> bool:
    """Évalue récursivement l'arbre de conditions d'une règle."""
    if isinstance(node, Condition):
        operator = get_operator(node.operator)
        return operator.apply(resolve(context, node.field), node.value, now)
    if isinstance(node, AllOf):
        return all(evaluate_condition(child, context, now) for child in node.all_)
    if isinstance(node, AnyOf):
        return any(evaluate_condition(child, context, now) for child in node.any_)
    return not evaluate_condition(node.not_, context, now)


def collect_observed(rule: Rule, context: dict[str, Any]) -> dict[str, Any]:
    """Rassemble les preuves attachées à l'évaluation.

    Les chemins testés par les conditions sont inclus en plus des `observe`
    déclarés : une alerte doit porter les valeurs qui l'ont déclenchée, sinon
    personne ne peut vérifier après coup pourquoi elle est née (F10).
    """
    observed: dict[str, Any] = {}
    for path in rule.referenced_fields:
        value = resolve(context, path)
        if not isinstance(value, MissingType):
            observed[path] = value
    return observed


def _evaluate(
    rule: Rule,
    context: dict[str, Any],
    *,
    mission_id: str,
    now: datetime,
    event_id: str | None,
) -> RuleEvaluation:
    matched = evaluate_condition(rule.when, context, now)

    if not matched:
        # Une évaluation non déclenchée est rendue elle aussi : c'est elle qui
        # autorise l'Alert Service à clore l'alerte correspondante (F13).
        return RuleEvaluation(
            rule_id=rule.id,
            rule_version=rule.version,
            mission_id=mission_id,
            matched=False,
            severity=rule.severity,
            category=rule.category,
            auto_resolve=rule.then.alert.auto_resolve if rule.then.alert else True,
            event_id=event_id,
            evaluated_at=now,
        )

    alert = rule.then.alert
    return RuleEvaluation(
        rule_id=rule.id,
        rule_version=rule.version,
        mission_id=mission_id,
        matched=True,
        action=rule.then.action,
        severity=rule.severity,
        category=rule.category,
        title=render(alert.title, context) if alert else None,
        message=render(alert.message, context) if alert else None,
        deduplication_key=render(alert.deduplication_key, context) if alert else None,
        cooldown_minutes=alert.cooldown_minutes if alert else 0,
        auto_resolve=alert.auto_resolve if alert else True,
        observed_data=collect_observed(rule, context),
        notify=tuple(
            NotificationRequest(
                role=target.role,
                user_id=target.user_id,
                management_level=target.management_level,
                channel=target.channel,
            )
            for target in rule.then.notify
        ),
        event_id=event_id,
        evaluated_at=now,
    )


def _by_severity_then_matched(evaluation: RuleEvaluation) -> tuple[int, int, str]:
    """Tri : déclenchements d'abord, par sévérité décroissante (F9).

    L'ordre est stabilisé par `rule_id` pour que deux cycles sur les mêmes
    données produisent exactement la même séquence — condition nécessaire pour
    rejouer un scénario et comparer les résultats.
    """
    severity_rank = evaluation.severity.rank if evaluation.severity else -1
    return (0 if evaluation.matched else 1, -severity_rank, evaluation.rule_id)


def evaluate_state_rules(rules: Iterable[Rule], snapshot: MissionSnapshot) -> list[RuleEvaluation]:
    """Évalue les règles d'état sur un snapshot de mission (F6).

    Appelée à chaque cycle. L'heure de référence est `snapshot.observed_at`,
    l'heure TMS de l'observation.
    """
    context = build_state_context(snapshot)
    now = snapshot.observed_at
    evaluations = [
        _evaluate(rule, context, mission_id=snapshot.mission_id, now=now, event_id=None)
        for rule in rules
        if rule.enabled and not rule.is_event_rule
    ]
    return sorted(evaluations, key=_by_severity_then_matched)


def evaluate_event_rules(
    rules: Iterable[Rule],
    snapshot: MissionSnapshot,
    event: MissionEvent,
) -> list[RuleEvaluation]:
    """Évalue les règles d'événement pour un événement nouveau (F7).

    À n'appeler qu'une fois par événement : l'appelant est responsable de
    marquer l'événement comme traité, et seulement après enregistrement des
    alertes, pour qu'un échec soit repris au cycle suivant.
    """
    context = build_event_context(snapshot, event)
    now = snapshot.observed_at
    evaluations = [
        _evaluate(rule, context, mission_id=snapshot.mission_id, now=now, event_id=event.id)
        for rule in rules
        if rule.enabled and rule.is_event_rule
    ]
    return sorted(evaluations, key=_by_severity_then_matched)


def triggered(evaluations: Sequence[RuleEvaluation]) -> list[RuleEvaluation]:
    """Ne garde que les évaluations déclenchées, dans l'ordre reçu."""
    return [evaluation for evaluation in evaluations if evaluation.matched]
