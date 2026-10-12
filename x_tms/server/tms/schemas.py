"""Contrat de l'API (Document 1 — Schéma d'API du faux TMS)."""

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

MissionStatus = Literal["planned", "in_progress", "completed", "cancelled"]
EventType = Literal[
    "arrived_pickup",
    "loading_started",
    "loading_completed",
    "departed_pickup",
    "arrived_delivery",
    "unloading_started",
    "unloading_completed",
    "driver_message",
    "deviation_detected",
]
EventSource = Literal["driver_app", "tms"]
VehicleStatus = Literal["idle", "moving", "stopped", "offline"]
DeviationSeverity = Literal["low", "medium", "high"]
DriverStatus = Literal["available", "on_mission"]

EVENT_TYPES: tuple[str, ...] = EventType.__args__  # type: ignore[attr-defined]


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Pagination ---------------------------------------------------------------------


class Pagination(Schema):
    cursor: str | None = Field(None, description="Curseur à repasser pour obtenir la page suivante.")
    has_more: bool


class Page(Schema, Generic[T]):
    data: list[T]
    pagination: Pagination


# --- Objets communs -----------------------------------------------------------------


class Place(Schema):
    label: str
    lat: float
    lon: float


class Point(Schema):
    lat: float
    lon: float


# --- Missions -----------------------------------------------------------------------


class MissionEventOut(Schema):
    id: str
    mission_id: str
    type: EventType
    occurred_at: datetime
    position: Point | None
    payload: dict[str, Any]
    source: EventSource


class MissionOut(Schema):
    id: str
    reference: str
    vehicle_id: str
    driver_id: str
    origin: Place
    destination: Place
    planned_route_id: str | None
    status: MissionStatus
    planned_pickup_at: datetime
    planned_delivery_at: datetime
    actual_pickup_at: datetime | None
    actual_delivery_at: datetime | None
    current_eta: datetime | None
    created_at: datetime
    updated_at: datetime


class MissionDetailOut(MissionOut):
    events: list[MissionEventOut]


class RouteWaypoint(Schema):
    lat: float
    lon: float
    sequence: int


class RouteOut(Schema):
    id: str
    mission_id: str
    waypoints: list[RouteWaypoint]
    polyline: str = Field(description="Polyline encodée au format Google (précision 1e5).")
    distance_km: float
    duration_min: float


class DeviationOut(Schema):
    mission_id: str
    current_offset_km: float | None = Field(
        description="Écart courant (km) entre le véhicule et l'itinéraire. Null tant que le véhicule n'a pas quitté le point de chargement."
    )
    current_offset_since: datetime | None = Field(
        description="Début de l'écart courant (null si le véhicule est sur l'itinéraire)."
    )
    duration_minutes: float | None = Field(description="Durée de l'écart courant, en minutes.")
    severity: DeviationSeverity | None
    vehicle_position: Point | None
    computed_at: datetime | None


class EtaOut(Schema):
    mission_id: str
    planned_delivery_at: datetime
    current_eta: datetime | None
    delay_minutes: float | None = Field(
        description="current_eta - planned_delivery_at, en minutes (négatif = avance)."
    )
    remaining_distance_km: float | None
    computed_at: datetime


# --- Véhicules ----------------------------------------------------------------------


class PositionOut(Schema):
    lat: float
    lon: float
    heading: float | None
    speed_kmh: float | None
    recorded_at: datetime


class PositionHistoryItem(PositionOut):
    mission_id: str | None


class VehicleOut(Schema):
    id: str
    plate: str
    fleet_id: str | None
    current_position: PositionOut | None
    status: VehicleStatus
    last_seen_at: datetime | None
    current_mission_id: str | None


class VehiclePositionOut(PositionOut):
    vehicle_id: str


# --- Utilisateurs -------------------------------------------------------------------


class UserOut(Schema):
    id: str
    name: str
    role: str
    management_level: str
    phone: str | None
    email: str | None


class DriverOut(UserOut):
    status: DriverStatus
    current_mission_id: str | None


# --- Meta ---------------------------------------------------------------------------


class SimulationInfo(Schema):
    running: bool
    run_id: str | None
    speed: float


class HealthOut(Schema):
    status: Literal["ok"]
    version: str
    time: datetime = Field(description="Heure courante du TMS (éventuellement accélérée par le simulateur).")
    simulation: SimulationInfo
