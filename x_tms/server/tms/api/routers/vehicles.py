from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from tms.api.deps import DEFAULT, CursorParam, LimitParam, SessionDep
from tms.clock import now
from tms.config import get_settings
from tms.errors import ProblemError, not_found
from tms.models import Vehicle, VehiclePosition
from tms.pagination import paginate
from tms.schemas import (
    Page,
    PositionHistoryItem,
    VehicleOut,
    VehiclePositionOut,
    VehicleStatus,
)
from tms.services import tracking
from tms.services.serializers import (
    position_history_item,
    vehicle_out,
    vehicle_position,
)

router = APIRouter(prefix="/vehicles", tags=["Véhicules"])


def _get_vehicle(session, vehicle_id: str) -> Vehicle:
    vehicle = session.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise not_found("Véhicule", vehicle_id)
    return vehicle


def _out(session, vehicle: Vehicle, at: datetime) -> VehicleOut:
    mission = tracking.active_mission_for_vehicle(session, vehicle.id)
    return vehicle_out(vehicle, at, mission.id if mission else None)


@router.get("", response_model=Page[VehicleOut], summary="Lister les véhicules")
def list_vehicles(
    session: SessionDep,
    status: VehicleStatus | None = None,
    fleet_id: str | None = None,
    limit: LimitParam = DEFAULT,
    cursor: CursorParam = None,
):
    at = now(session)
    stmt = select(Vehicle).order_by(Vehicle.id)
    if fleet_id:
        stmt = stmt.where(Vehicle.fleet_id == fleet_id)
    if status:
        # `offline` est déduit de l'ancienneté de la dernière position.
        threshold = at - timedelta(minutes=get_settings().offline_after_minutes)
        if status == "offline":
            stmt = stmt.where(Vehicle.status != "idle", Vehicle.last_seen_at < threshold)
        elif status == "idle":
            stmt = stmt.where(Vehicle.status == "idle")
        else:
            stmt = stmt.where(
                Vehicle.status == status,
                or_(Vehicle.last_seen_at.is_(None), Vehicle.last_seen_at >= threshold),
            )
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[_out(session, v, at) for v in rows], pagination=pagination)


@router.get("/{vehicle_id}", response_model=VehicleOut, summary="Détail d'un véhicule")
def get_vehicle(vehicle_id: str, session: SessionDep):
    return _out(session, _get_vehicle(session, vehicle_id), now(session))


@router.get(
    "/{vehicle_id}/position",
    response_model=VehiclePositionOut,
    summary="Dernière position connue",
)
def get_position(vehicle_id: str, session: SessionDep):
    vehicle = _get_vehicle(session, vehicle_id)
    position = vehicle_position(vehicle)
    if position is None:
        raise ProblemError(
            404,
            "Position inconnue",
            f"Aucune position reçue pour le véhicule {vehicle_id}.",
        )
    return VehiclePositionOut(vehicle_id=vehicle.id, **position.model_dump())


@router.get(
    "/{vehicle_id}/positions",
    response_model=Page[PositionHistoryItem],
    summary="Historique des positions",
)
def list_positions(
    vehicle_id: str,
    session: SessionDep,
    since: Annotated[datetime | None, Query(description="Positions enregistrées après cette date.")] = None,
    until: Annotated[datetime | None, Query(description="Positions enregistrées avant cette date.")] = None,
    mission_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    cursor: CursorParam = None,
):
    _get_vehicle(session, vehicle_id)
    stmt = (
        select(VehiclePosition)
        .where(VehiclePosition.vehicle_id == vehicle_id)
        .order_by(VehiclePosition.recorded_at, VehiclePosition.pk)
    )
    if since:
        stmt = stmt.where(VehiclePosition.recorded_at > since)
    if until:
        stmt = stmt.where(VehiclePosition.recorded_at <= until)
    if mission_id:
        stmt = stmt.where(VehiclePosition.mission_id == mission_id)
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[position_history_item(p) for p in rows], pagination=pagination)
