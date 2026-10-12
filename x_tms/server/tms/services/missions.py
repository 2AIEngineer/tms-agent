"""Missions et chronologie d'événements (écrits uniquement par les systèmes internes du TMS : simulateur)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from tms.errors import not_found
from tms.ids import new_id
from tms.models import Mission, MissionEvent


def get_mission(session: Session, mission_id: str) -> Mission:
    mission = session.get(Mission, mission_id)
    if mission is None:
        raise not_found("Mission", mission_id)
    return mission


def mission_events(session: Session, mission_id: str) -> list[MissionEvent]:
    return list(
        session.scalars(
            select(MissionEvent)
            .where(MissionEvent.mission_id == mission_id)
            .order_by(MissionEvent.occurred_at, MissionEvent.pk)
        )
    )


def _touch(session: Session, mission: Mission, at: datetime) -> None:
    mission.updated_at = max(mission.updated_at, at)
    session.flush()


def add_event(
    session: Session,
    mission: Mission,
    *,
    type_: str,
    occurred_at: datetime,
    source: str,
    lat: float | None = None,
    lon: float | None = None,
    payload: dict | None = None,
) -> MissionEvent:
    evt = MissionEvent(
        id=new_id("evt"),
        mission_id=mission.id,
        type=type_,
        occurred_at=occurred_at,
        lat=lat,
        lon=lon,
        payload=payload or {},
        source=source,
    )
    session.add(evt)

    if type_ == "arrived_pickup":
        mission.actual_pickup_at = mission.actual_pickup_at or occurred_at
        if mission.status == "planned":
            mission.status = "in_progress"
    elif type_ == "departed_pickup":
        mission.departed_pickup_at = mission.departed_pickup_at or occurred_at
    elif type_ == "arrived_delivery":
        mission.actual_delivery_at = mission.actual_delivery_at or occurred_at
        mission.current_eta = mission.actual_delivery_at
    elif type_ == "unloading_completed":
        mission.status = "completed"

    _touch(session, mission, occurred_at)
    return evt


def set_status(session: Session, mission: Mission, status: str, at: datetime) -> None:
    mission.status = status
    _touch(session, mission, at)
