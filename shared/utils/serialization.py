"""Conversion de valeurs Python vers des structures JSON.

`observed_data` d'une alerte contient ce que les règles ont lu dans le
snapshot : des dates, des énumérations, des positions. Rien de tout cela n'est
sérialisable tel quel, et c'est pourtant ce qui part en JSONB, dans l'API et
dans le prompt de l'enquêteur. La conversion est centralisée pour que les trois
voient exactement la même chose.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID


def parse_datetime(value: Any) -> datetime | None:
    """Lit une date venue d'un TMS, ou rend `None` si elle est inexploitable.

    Tolérant par nécessité : un TMS tiers peut rendre un champ absent, vide ou
    mal formé. Lever ici ferait échouer tout le snapshot d'une mission à cause
    d'un seul horodatage, alors que la spécification demande de signaler la
    partie manquante et de continuer le cycle (F4).

    Le suffixe « Z » est accepté : `fromisoformat` ne le gère qu'à partir de
    Python 3.11 pour certaines formes, et les API REST l'emploient couramment.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def to_jsonable(value: Any) -> Any:
    """Rend une valeur sérialisable en JSON, en préservant son sens.

    Les dates partent en ISO 8601 avec leur fuseau : une heure TMS stockée sans
    fuseau serait relue comme une heure locale, ce qui fausserait toute analyse
    postérieure de l'alerte.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence | set | frozenset):
        return [to_jsonable(item) for item in value]
    # Modèles Pydantic et objets porteurs d'un `model_dump`.
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return to_jsonable(dump())
    return str(value)
