"""Opérateurs géospatiaux : distance et appartenance à une zone.

Les positions observées sont des mappings `{lat, lon, ...}` tels que le
contexte d'évaluation les expose. Une position absente ou incomplète rend la
condition fausse.
"""

from datetime import datetime
from typing import Any

from shared.utils.geo import haversine_km

from .base import InvalidRuleValue, Operator, register, require_number

__all__ = ["haversine_km", "point_in_polygon"]


def _coordinates(observed: Any) -> tuple[float, float] | None:
    """Extrait (lat, lon) d'une position observée, ou None si inexploitable."""
    if isinstance(observed, dict):
        lat, lon = observed.get("lat"), observed.get("lon")
    else:
        lat, lon = getattr(observed, "lat", None), getattr(observed, "lon", None)
    if isinstance(lat, int | float) and isinstance(lon, int | float):
        return float(lat), float(lon)
    return None


def _validate_point_and_distance(value: Any) -> None:
    if not isinstance(value, dict):
        raise InvalidRuleValue(f"'distance_greater_than_km' attend {{lat, lon, km}}, reçu {value!r}")
    missing = {"lat", "lon", "km"} - set(value)
    if missing:
        raise InvalidRuleValue(
            f"'distance_greater_than_km' : clés manquantes {sorted(missing)} dans {value!r}"
        )
    for key in ("lat", "lon", "km"):
        require_number(value[key], "distance_greater_than_km")
    _validate_latlon(value["lat"], value["lon"])


def _distance_greater_than(observed: Any, expected: Any, now: datetime) -> bool:
    point = _coordinates(observed)
    if point is None:
        return False
    return haversine_km(point[0], point[1], float(expected["lat"]), float(expected["lon"])) > float(
        expected["km"]
    )


def _validate_latlon(lat: Any, lon: Any) -> None:
    if not -90 <= float(lat) <= 90:
        raise InvalidRuleValue(f"Latitude hors bornes : {lat!r}")
    if not -180 <= float(lon) <= 180:
        raise InvalidRuleValue(f"Longitude hors bornes : {lon!r}")


def _validate_polygon(value: Any) -> None:
    if not isinstance(value, list | tuple) or len(value) < 3:
        raise InvalidRuleValue(f"Un polygone demande au moins 3 sommets [[lat, lon], ...], reçu {value!r}")
    for vertex in value:
        if not isinstance(vertex, list | tuple) or len(vertex) != 2:
            raise InvalidRuleValue(f"Sommet invalide : {vertex!r}, attendu [lat, lon]")
        require_number(vertex[0], "polygone")
        require_number(vertex[1], "polygone")
        _validate_latlon(vertex[0], vertex[1])


def point_in_polygon(lat: float, lon: float, polygon: list[Any]) -> bool:
    """Lancer de rayon : compte les intersections avec les arêtes du polygone.

    Le polygone est traité en coordonnées planes. À l'échelle d'une zone
    d'exploitation (quelques dizaines de kilomètres), l'erreur de projection
    est très inférieure à la précision des zones que l'exploitation dessine.
    """
    inside = False
    count = len(polygon)
    for index in range(count):
        lat_a, lon_a = float(polygon[index][0]), float(polygon[index][1])
        lat_b, lon_b = float(polygon[index - 1][0]), float(polygon[index - 1][1])
        straddles = (lon_a > lon) != (lon_b > lon)
        if straddles and lat < (lat_b - lat_a) * (lon - lon_a) / (lon_b - lon_a) + lat_a:
            inside = not inside
    return inside


def _inside_polygon(observed: Any, expected: Any, now: datetime) -> bool:
    point = _coordinates(observed)
    if point is None:
        return False
    return point_in_polygon(point[0], point[1], list(expected))


def _outside_polygon(observed: Any, expected: Any, now: datetime) -> bool:
    point = _coordinates(observed)
    # Position inconnue : on ne déclare pas le véhicule hors zone. Une absence
    # de donnée n'est pas une sortie de zone.
    if point is None:
        return False
    return not point_in_polygon(point[0], point[1], list(expected))


register(
    Operator(
        name="distance_greater_than_km",
        validate=_validate_point_and_distance,
        apply=_distance_greater_than,
    )
)
register(Operator(name="inside_polygon", validate=_validate_polygon, apply=_inside_polygon))
register(Operator(name="outside_polygon", validate=_validate_polygon, apply=_outside_polygon))
