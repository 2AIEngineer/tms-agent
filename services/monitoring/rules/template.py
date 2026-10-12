"""Rendu des gabarits `{{ chemin }}` des règles.

Substitution de chemins uniquement : pas d'expression, pas d'appel, pas de
boucle. Les règles sont écrites par l'équipe métier et rechargées à chaud —
un moteur de gabarit complet y ferait entrer du code exécutable dans un
fichier de configuration, ce qui est exactement ce qu'il faut éviter.
"""

import re
from datetime import datetime
from typing import Any

from .operators import MissingType
from .paths import resolve

# `{{ chemin.pointé }}`, espaces optionnels. Volontairement restrictif : tout
# ce qui n'est pas un chemin simple est laissé tel quel, donc visible à la
# relecture plutôt qu'interprété.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

# Ce qui s'affiche à la place d'un champ que le TMS n'a pas renseigné. Préféré à
# une chaîne vide : un message qui dit « n/a » se diagnostique, un trou blanc non.
MISSING_PLACEHOLDER = "n/a"


def format_value(value: Any) -> str:
    """Met en forme une valeur observée pour un message lisible par un humain.

    Les flottants du TMS arrivent avec une précision inutile (7.234999 km) :
    deux décimales suffisent pour un message d'alerte, et la donnée brute
    reste disponible dans `observed_data`.
    """
    if isinstance(value, MissingType) or value is None:
        return MISSING_PLACEHOLDER
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, float):
        rounded = round(value, 2)
        return str(int(rounded)) if rounded == int(rounded) else str(rounded)
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    return str(value)


def render(template: str, context: dict[str, Any]) -> str:
    """Remplace chaque `{{ chemin }}` par sa valeur mise en forme."""

    def substitute(match: re.Match[str]) -> str:
        return format_value(resolve(context, match.group(1)))

    # Les messages YAML multilignes (bloc `>`) arrivent avec des retours à la
    # ligne de pliage : on les ramène à une ligne, puisque le message part en
    # SMS, en notification in-app ou dans un appel vocal.
    return " ".join(PLACEHOLDER.sub(substitute, template).split())
