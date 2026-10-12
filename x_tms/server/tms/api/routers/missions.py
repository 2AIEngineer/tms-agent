from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from tms.api.deps import DEFAULT, CursorParam, LimitParam, SessionDep
from tms.clock import now
from tms.errors import not_found
from tms.models import Mission, MissionEvent, Route, Vehicle
from tms.pagination import paginate
from tms.schemas import (
    DeviationOut,
    EtaOut,
    EventType,
    MissionDetailOut,
    MissionEventOut,
    MissionOut,
    MissionStatus,
    Page,
    RouteOut,
)
from tms.services import missions as mission_service
from tms.services import tracking
from tms.services.serializers import event_out, mission_detail_out, mission_out

router = APIRouter(prefix="/missions", tags=["Missions"])


@router.get("", response_model=Page[MissionOut], summary="Lister les missions")
def list_missions(
    session: SessionDep,
    status: Annotated[list[MissionStatus] | None, Query(description="Filtre(s) sur le statut.")] = None,
    vehicle_id: str | None = None,
    driver_id: str | None = None,
    updated_since: Annotated[
        datetime | None,
        Query(description="Missions modifiées après cette date (ISO 8601)."),
    ] = None,
    limit: LimitParam = DEFAULT,
    cursor: CursorParam = None,
):
    stmt = select(Mission).order_by(Mission.planned_pickup_at.desc(), Mission.id)
    if status:
        stmt = stmt.where(Mission.status.in_(status))
    if vehicle_id:
        stmt = stmt.where(Mission.vehicle_id == vehicle_id)
    if driver_id:
        stmt = stmt.where(Mission.driver_id == driver_id)
    if updated_since:
        stmt = stmt.where(Mission.updated_at > updated_since)
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[mission_out(m) for m in rows], pagination=pagination)


@router.get("/{mission_id}", response_model=MissionDetailOut, summary="Détail d'une mission")
def get_mission(mission_id: str, session: SessionDep):
    mission = mission_service.get_mission(session, mission_id)
    return mission_detail_out(mission, mission_service.mission_events(session, mission_id))


@router.get(
    "/{mission_id}/events",
    response_model=Page[MissionEventOut],
    summary="Chronologie des événements",
)
def list_events(
    mission_id: str,
    session: SessionDep,
    type: Annotated[list[EventType] | None, Query(description="Filtre(s) sur le type d'événement.")] = None,
    since: Annotated[datetime | None, Query(description="Événements survenus après cette date.")] = None,
    limit: LimitParam = DEFAULT,
    cursor: CursorParam = None,
):
    mission_service.get_mission(session, mission_id)
    stmt = (
        select(MissionEvent)
        .where(MissionEvent.mission_id == mission_id)
        .order_by(MissionEvent.occurred_at, MissionEvent.pk)
    )
    if type:
        stmt = stmt.where(MissionEvent.type.in_(type))
    if since:
        stmt = stmt.where(MissionEvent.occurred_at > since)
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[event_out(e) for e in rows], pagination=pagination)


@router.get("/{mission_id}/route", response_model=RouteOut, summary="Itinéraire planifié")
def get_route(mission_id: str, session: SessionDep):
    mission = mission_service.get_mission(session, mission_id)
    route = session.get(Route, mission.planned_route_id) if mission.planned_route_id else None
    if route is None:
        raise not_found("Itinéraire", f"mission {mission_id}")
    return RouteOut.model_validate(route)


@router.get(
    "/{mission_id}/deviation",
    response_model=DeviationOut,
    summary="Écart courant vs itinéraire",
)
def get_deviation(mission_id: str, session: SessionDep):
    mission = mission_service.get_mission(session, mission_id)
    return tracking.deviation_for(session, mission, session.get(Vehicle, mission.vehicle_id))


@router.get("/{mission_id}/eta", response_model=EtaOut, summary="ETA recalculée")
def get_eta(mission_id: str, session: SessionDep):
    mission = mission_service.get_mission(session, mission_id)
    return tracking.eta_for(session, mission, now(session))
