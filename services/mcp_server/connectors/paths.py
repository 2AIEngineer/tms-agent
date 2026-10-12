"""Lecture d'une valeur dans un payload JSON par chemin pointé.

Un seul mécanisme d'extraction pour tout le mapping. Les chemins viennent d'un
fichier de dialecte écrit à la main : ils doivent donc tolérer l'absence sans
lever, exactement comme les mappers le font déjà pour les champs manquants.
"""

from typing import Any


def extract(payload: Any, path: str) -> Any:
    """Lit `path` (ex. `from.name`, `attributes.position.lat`) dans un payload.

    Rend `None` dès qu'un segment est introuvable. Absence et valeur nulle sont
    volontairement confondues ici : du point de vue du mapping, « le TMS n'a pas
    ce champ » et « le TMS l'a rendu vide » appellent la même décision — ne rien
    inventer. La distinction utile est faite plus haut, par `missing_parts` du
    snapshot, au niveau d'une ressource entière.

    Un segment numérique indexe une liste (`stops.0.label`), ce qui permet de
    pointer la première étape d'une tournée chez un TMS multi-arrêts.
    """
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if -len(current) <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current
