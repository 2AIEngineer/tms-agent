"""Opérateurs du langage de règles.

Importer ce paquet enregistre toutes les familles d'opérateurs : le registre
est ainsi complet dès que le chargeur ou le moteur l'interroge.
"""

from . import comparison, geospatial, temporal  # noqa: F401  (import pour effet d'enregistrement)
from .base import MISSING, InvalidRuleValue, MissingType, Operator, get, names, register

__all__ = [
    "MISSING",
    "InvalidRuleValue",
    "MissingType",
    "Operator",
    "get",
    "names",
    "register",
]
