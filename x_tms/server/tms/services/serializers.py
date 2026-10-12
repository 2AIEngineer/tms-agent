"""Conversion modèles ORM -> schémas de l'API."""

from datetime import datetime, timedelta

from tms.config import get_settings
from tms.models import Mission, MissionEvent, Vehicle, VehiclePosition
from tms.schemas import (
    MissionDetailOut,
    MissionEventOut,
    MissionOut,
    Point,
    PositionHistoryItem,
    PositionOut,
    VehicleOut,
)


def mission_out(m: Mission) -> MissionOut:
    return MissionOut.model_validate(m)


def mission_detail_out(m: Mission, events: list[MissionEvent]) -> MissionDetailOut:
    return MissionDetailOut(**mission_out(m).model_dump(), events=[event_out(e) for e in events])


def event_out(e: MissionEvent) -> MissionEventOut:
    position = Point(lat=e.lat, lon=e.lon) if e.lat is not None and e.lon is not None else None
    return MissionEventOut(
        id=e.id,
        mission_id=e.mission_id,
        type=e.type,
        occurred_at=e.occurred_at,
        position=position,
        payload=e.payload or {},
        source=e.source,
    )


def vehicle_position(v: Vehicle) -> PositionOut | None:
    if v.lat is None or v.lon is None or v.position_recorded_at is None:
        return None
    return PositionOut(
        lat=v.lat, lon=v.lon, heading=v.heading, speed_kmh=v.speed_kmh, recorded_at=v.position_recorded_at
    )


def effective_vehicle_status(v: Vehicle, now: datetime) -> str:
    """`offline` : un véhicule en activité qui n'émet plus de position (un véhicule `idle` est garé)."""
    stale = v.last_seen_at is not None and now - v.last_seen_at > timedelta(
        minutes=get_settings().offline_after_minutes
    )
    return "offline" if stale and v.status != "idle" else v.status


def vehicle_out(v: Vehicle, now: datetime, current_mission_id: str | None) -> VehicleOut:
    return VehicleOut(
        id=v.id,
        plate=v.plate,
        fleet_id=v.fleet_id,
        current_position=vehicle_position(v),
        status=effective_vehicle_status(v, now),
        last_seen_at=v.last_seen_at,
        current_mission_id=current_mission_id,
    )


def position_history_item(p: VehiclePosition) -> PositionHistoryItem:
    return PositionHistoryItem.model_validate(p)
