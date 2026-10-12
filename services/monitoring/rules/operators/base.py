"""Socle du registre d'opérateurs.

Un opérateur est une paire de fonctions : l'une valide la valeur écrite dans le
YAML **au chargement**, l'autre l'applique à une valeur observée. Cette
séparation est le cœur du contrat passé à l'équipe métier : une règle mal
écrite est refusée au dépôt du fichier, pas découverte six heures plus tard au
milieu d'un cycle de surveillance.

Pas de hiérarchie de classes ici : un opérateur est une donnée, pas un objet à
spécialiser.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final


class MissingType:
    """Sentinelle d'absence, distincte de `None`.

    `None` est une valeur légitime du TMS (« pas d'écart calculé »). Les
    confondre empêcherait de distinguer un champ absent du contexte d'un champ
    présent mais vide.
    """

    _instance: "MissingType | None" = None

    def __new__(cls) -> "MissingType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = MissingType()


class InvalidRuleValue(ValueError):
    """Valeur inutilisable pour cet opérateur, détectée au chargement de la règle."""


@dataclass(frozen=True, slots=True)
class Operator:
    """Un opérateur du langage de règles.

    - `validate` : lève `InvalidRuleValue` si le YAML est inexploitable.
    - `apply` : rend le verdict. Reçoit `now` en **heure TMS**, que l'opérateur
      soit temporel ou non, pour que la signature reste uniforme.
    """

    name: str
    validate: Callable[[Any], None]
    apply: Callable[[Any, Any, datetime], bool]


_REGISTRY: dict[str, Operator] = {}


def register(operator: Operator) -> Operator:
    if operator.name in _REGISTRY:
        raise RuntimeError(f"Opérateur déjà enregistré : {operator.name}")
    _REGISTRY[operator.name] = operator
    return operator


def get(name: str) -> Operator:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise InvalidRuleValue(f"Opérateur inconnu : '{name}'. Opérateurs disponibles : {known}") from None


def names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# --- Aides partagées par plusieurs familles d'opérateurs ----------------------


def require_number(value: Any, operator_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidRuleValue(f"'{operator_name}' attend un nombre, reçu {value!r}")


def as_number(value: Any) -> float | None:
    """Convertit une valeur observée en nombre, ou rend None si impossible.

    Une valeur non numérique n'est pas une erreur : elle rend la condition
    fausse, comme un champ absent.
    """
    if isinstance(value, bool | MissingType):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
