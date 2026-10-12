"""Résolution d'un chemin pointé dans le contexte d'évaluation.

Un seul mécanisme de lecture pour les conditions, les champs `observe` et les
gabarits de message : si les trois divergeaient, une règle pourrait tester un
champ et en afficher un autre.
"""

from typing import Any

from .operators import MISSING


def resolve(context: dict[str, Any], path: str) -> Any:
    """Lit `path` (ex. `event.payload.severity`) dans le contexte.

    Rend `MISSING` dès qu'un segment est introuvable. Aucune exception : un
    champ absent rend la condition fausse, ce qui est le comportement attendu
    par la spécification plutôt qu'un cycle interrompu.
    """
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, dict):
            if segment not in current:
                return MISSING
            current = current[segment]
        else:
            return MISSING
    return current
