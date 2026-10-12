"""Positions GPS, écart à l'itinéraire et ETA : faits calculés par le TMS."""

from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from tms.config import get_settings
from tms.geo import RoutePath
from tms.models import Mission, Route, Vehicle, VehiclePosition
from tms.schemas import DeviationOut, EtaOut, Point
from tms.services.serializers import vehicle_position

MOVING_SPEED_KMH = 3.0


@lru_cache(maxsize=256)
def _route_path(route_id: str, polyline_points: tuple[tuple[float, float], ...]) -> RoutePath:
    return RoutePath(polyline_points)


def route_path(route: Route) -> RoutePath:
    points = tuple((w["lat"], w["lon"]) for w in sorted(route.waypoints, key=lambda w: w["sequence"]))
    return _route_path(route.id, points)


def active_mission_for_vehicle(session: Session, vehicle_id: str) -> Mission | None:
    return session.scalars(
        select(Mission)
        .where(Mission.vehicle_id == vehicle_id, Mission.status == "in_progress")
        .order_by(Mission.planned_pickup_at.desc())
    ).first()


def deviation_severity(offset_km: float) -> str:
    if offset_km < 2:
        return "low"
    if offset_km < 10:
        return "medium"
    return "high"


def compute_eta(mission: Mission, route: Route | None, now: datetime) -> tuple[datetime | None, float | None]:
    """ETA d'arrivée au point de livraison et distance restante (km)."""
    if mission.actual_delivery_at is not None:
        return mission.actual_delivery_at, 0.0
    if mission.status == "cancelled" or route is None:
        return mission.current_eta, None
    minutes_per_km = route.duration_min / route.distance_km if route.distance_km else 0
    if mission.departed_pickup_at is None or mission.route_progress_km is None:
        # Pas encore parti : on considère le plan tenable tant que le temps de trajet le permet.
        eta = max(mission.planned_delivery_at, now + timedelta(minutes=route.duration_min))
        return eta, route.distance_km
    path = route_path(route)
    remaining_ratio = max(0.0, 1 - mission.route_progress_km / path.length_km)
    remaining_km = remaining_ratio * route.distance_km + (mission.deviation_offset_km or 0.0)
    eta = now + timedelta(minutes=remaining_km * minutes_per_km)
    return eta.replace(microsecond=0), remaining_km


def record_position(
    session: Session,
    vehicle: Vehicle,
    *,
    lat: float,
    lon: float,
    heading: float | None,
    speed_kmh: float | None,
    recorded_at: datetime,
) -> VehiclePosition:
    mission = active_mission_for_vehicle(session, vehicle.id)
    position = VehiclePosition(
        vehicle_id=vehicle.id,
        mission_id=mission.id if mission else None,
        lat=lat,
        lon=lon,
        heading=heading,
        speed_kmh=speed_kmh,
        recorded_at=recorded_at,
    )
    session.add(position)
    vehicle.lat, vehicle.lon, vehicle.heading, vehicle.speed_kmh = lat, lon, heading, speed_kmh
    vehicle.position_recorded_at = recorded_at
    vehicle.last_seen_at = recorded_at
    vehicle.status = "moving" if (speed_kmh or 0) >= MOVING_SPEED_KMH else "stopped"

    if mission is not None:
        _update_mission_tracking(session, mission, (lat, lon), recorded_at)

    session.flush()
    return position


def _update_mission_tracking(
    session: Session, mission: Mission, point: tuple[float, float], at: datetime
) -> None:
    route = session.get(Route, mission.planned_route_id) if mission.planned_route_id else None
    if route is not None and mission.departed_pickup_at is not None and mission.actual_delivery_at is None:
        projection = route_path(route).project(point)
        offset = round(projection.offset_km, 2)
        if offset > get_settings().on_route_tolerance_km:
            mission.deviation_since = mission.deviation_since or at
        else:
            mission.deviation_since = None
        mission.deviation_offset_km = offset
        mission.deviation_computed_at = at
        # La progression ne recule pas (bruit GPS, détours).
        mission.route_progress_km = max(mission.route_progress_km or 0.0, projection.along_km)
    eta, _ = compute_eta(mission, route, at)
    mission.current_eta = eta
    mission.updated_at = at


def deviation_for(session: Session, mission: Mission, vehicle: Vehicle) -> DeviationOut:
    offset = mission.deviation_offset_km
    since = mission.deviation_since
    computed = mission.deviation_computed_at
    duration = None
    if offset is not None:
        duration = round((computed - since).total_seconds() / 60, 1) if since and computed else 0.0
    current = vehicle_position(vehicle)
    return DeviationOut(
        mission_id=mission.id,
        current_offset_km=offset,
        current_offset_since=since,
        duration_minutes=duration,
        severity=deviation_severity(offset) if offset is not None else None,
        vehicle_position=Point(lat=current.lat, lon=current.lon) if current else None,
        computed_at=computed,
    )


def eta_for(session: Session, mission: Mission, now: datetime) -> EtaOut:
    route = session.get(Route, mission.planned_route_id) if mission.planned_route_id else None
    eta, remaining = compute_eta(mission, route, now)
    delay = round((eta - mission.planned_delivery_at).total_seconds() / 60, 1) if eta else None
    return EtaOut(
        mission_id=mission.id,
        planned_delivery_at=mission.planned_delivery_at,
        current_eta=eta,
        delay_minutes=delay,
        remaining_distance_km=round(remaining, 2) if remaining is not None else None,
        computed_at=now,
    )
