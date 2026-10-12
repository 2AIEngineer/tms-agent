"""Opérateurs temporels, évalués en heure TMS.

`now` n'est jamais `datetime.now()` : c'est l'heure du TMS portée par le
snapshot. Le simulateur accélère son horloge et peut la remettre à zéro ; une
règle qui lirait l'horloge locale deviendrait fausse dès la première
accélération (F3).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from .base import InvalidRuleValue, MissingType, Operator, as_number, register, require_number


def _as_aware(moment: datetime) -> datetime:
    """Ramène un instant naïf en UTC.

    Certains TMS rendent des dates sans fuseau. Les soustraire à un instant
    conscient lèverait une TypeError au milieu d'un cycle ; on suppose UTC, ce
    qui est la convention des API de transport, plutôt que d'échouer.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _age_minutes(observed: Any, now: datetime) -> float | None:
    """Âge en minutes d'un instant observé, ou None si ce n'est pas une date."""
    if isinstance(observed, MissingType) or not isinstance(observed, datetime):
        return None
    return (_as_aware(now) - _as_aware(observed)).total_seconds() / 60.0


def _older_than(observed: Any, expected: Any, now: datetime) -> bool:
    age = _age_minutes(observed, now)
    return age is not None and age > float(expected)


def _newer_than(observed: Any, expected: Any, now: datetime) -> bool:
    age = _age_minutes(observed, now)
    return age is not None and age < float(expected)


def _duration_greater_than(observed: Any, expected: Any, now: datetime) -> bool:
    """Compare une durée à un seuil en minutes.

    Accepte trois formes, parce que les TMS expriment les durées de trois
    façons et que l'équipe métier ne doit pas avoir à savoir laquelle :
      - un nombre        -> des minutes déjà calculées (`deviation.duration_minutes`) ;
      - un `timedelta`   -> une durée native ;
      - un `datetime`    -> la durée écoulée depuis cet instant jusqu'à `now`.
    """
    threshold = float(expected)

    if isinstance(observed, timedelta):
        return observed.total_seconds() / 60.0 > threshold
    if isinstance(observed, datetime):
        age = _age_minutes(observed, now)
        return age is not None and age > threshold
    minutes = as_number(observed)
    return minutes is not None and minutes > threshold


def _validate_positive_minutes(value: Any) -> None:
    require_number(value, "opérateur temporel")
    if float(value) < 0:
        raise InvalidRuleValue(f"Un seuil de durée ne peut pas être négatif : {value!r}")


register(Operator(name="older_than_minutes", validate=_validate_positive_minutes, apply=_older_than))
register(Operator(name="newer_than_minutes", validate=_validate_positive_minutes, apply=_newer_than))
register(
    Operator(
        name="duration_greater_than_minutes",
        validate=_validate_positive_minutes,
        apply=_duration_greater_than,
    )
)
