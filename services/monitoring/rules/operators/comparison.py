"""Opérateurs de comparaison et d'appartenance.

Règle commune : une valeur observée absente, nulle ou d'un type inattendu rend
la condition **fausse**, jamais en erreur. Un TMS qui cesse de renseigner un
champ ne doit pas faire tomber le cycle de surveillance (F4).
"""

from datetime import datetime
from typing import Any

from .base import (
    MISSING,
    InvalidRuleValue,
    MissingType,
    Operator,
    as_number,
    register,
    require_number,
)


def _no_validation(value: Any) -> None:
    """`equals` et `not_equals` acceptent tout scalaire : rien à valider."""


def _present(observed: Any) -> bool:
    return not isinstance(observed, MissingType) and observed is not None


def _equals(observed: Any, expected: Any, now: datetime) -> bool:
    if not _present(observed):
        return False
    # Les énumérations du pivot sont des StrEnum : la comparaison à la chaîne du
    # YAML fonctionne directement, sans conversion.
    return bool(observed == expected)


def _not_equals(observed: Any, expected: Any, now: datetime) -> bool:
    # Volontairement faux sur un champ absent : « différent de X » ne doit pas
    # devenir vrai simplement parce que la donnée manque, sinon toute panne de
    # lecture TMS déclencherait des alertes en masse.
    if not _present(observed):
        return False
    return bool(observed != expected)


def _numeric_comparison(name: str, compare: Any) -> Operator:
    def apply(observed: Any, expected: Any, now: datetime) -> bool:
        left = as_number(observed)
        right = as_number(expected)
        if left is None or right is None:
            return False
        return bool(compare(left, right))

    return Operator(name=name, validate=lambda v: require_number(v, name), apply=apply)


def _validate_pair(value: Any) -> None:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise InvalidRuleValue(f"'between' attend [min, max], reçu {value!r}")
    for bound in value:
        require_number(bound, "between")
    if float(value[0]) > float(value[1]):
        raise InvalidRuleValue(f"'between' : borne basse supérieure à la borne haute ({value!r})")


def _between(observed: Any, expected: Any, now: datetime) -> bool:
    left = as_number(observed)
    if left is None:
        return False
    low, high = float(expected[0]), float(expected[1])
    return low <= left <= high


def _validate_collection(value: Any) -> None:
    if not isinstance(value, list | tuple) or not value:
        raise InvalidRuleValue(f"'in' / 'not_in' attend une liste non vide, reçu {value!r}")


def _in(observed: Any, expected: Any, now: datetime) -> bool:
    if not _present(observed):
        return False
    return observed in expected


def _not_in(observed: Any, expected: Any, now: datetime) -> bool:
    if not _present(observed):
        return False
    return observed not in expected


register(Operator(name="equals", validate=_no_validation, apply=_equals))
register(Operator(name="not_equals", validate=_no_validation, apply=_not_equals))
register(_numeric_comparison("greater_than", lambda a, b: a > b))
register(_numeric_comparison("greater_or_equal", lambda a, b: a >= b))
register(_numeric_comparison("less_than", lambda a, b: a < b))
register(_numeric_comparison("less_or_equal", lambda a, b: a <= b))
register(Operator(name="between", validate=_validate_pair, apply=_between))
register(Operator(name="in", validate=_validate_collection, apply=_in))
register(Operator(name="not_in", validate=_validate_collection, apply=_not_in))

__all__ = ["MISSING"]
