"""Construction de l'itinéraire planifié : tracé statique ou OSRM (routes réelles)."""

import logging
from dataclasses import dataclass

import httpx

from tms.geo import LatLon, RoutePath, decode_polyline, densify

log = logging.getLogger(__name__)

TRUCK_AVG_SPEED_KMH = 75.0
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"
OSRM_TRUCK_FACTOR = 1.2  # OSRM calcule des durées voiture


@dataclass(frozen=True)
class PlannedRoute:
    points: list[LatLon]
    distance_km: float
    duration_min: float


def _downsample(points: list[LatLon], max_points: int = 600) -> list[LatLon]:
    if len(points) <= max_points:
        return points
    step = len(points) / (max_points - 1)
    out = [points[int(i * step)] for i in range(max_points - 1)]
    out.append(points[-1])
    return out


def static_route(waypoints: tuple[LatLon, ...]) -> PlannedRoute:
    points = densify(waypoints, step_km=2.0)
    distance = RoutePath(points).length_km
    return PlannedRoute(points, round(distance, 2), round(distance / TRUCK_AVG_SPEED_KMH * 60, 1))


def osrm_route(waypoints: tuple[LatLon, ...]) -> PlannedRoute:
    coords = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    resp = httpx.get(
        OSRM_URL.format(coords=coords), params={"overview": "full", "geometries": "polyline"}, timeout=10
    )
    resp.raise_for_status()
    route = resp.json()["routes"][0]
    points = _downsample(decode_polyline(route["geometry"]))
    return PlannedRoute(
        points, round(route["distance"] / 1000, 2), round(route["duration"] / 60 * OSRM_TRUCK_FACTOR, 1)
    )


def plan_route(waypoints: tuple[LatLon, ...], mode: str) -> PlannedRoute:
    if mode == "osrm":
        try:
            return osrm_route(waypoints)
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            log.warning("OSRM indisponible (%s), repli sur le tracé statique", exc)
    return static_route(waypoints)
