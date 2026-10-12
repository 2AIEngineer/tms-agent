from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import Mapped, mapped_column

from tms.db import Base


class UTCDateTime(TypeDecorator):
    """Stocke en UTC naïf, restitue des datetimes UTC « aware »."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime naïf refusé : utiliser des datetimes UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, index=True)
    management_level: Mapped[str] = mapped_column(String, index=True)
    phone: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    plate: Mapped[str] = mapped_column(String)
    fleet_id: Mapped[str | None] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="idle")
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    heading: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[float | None] = mapped_column(Float)
    position_recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class VehiclePosition(Base):
    __tablename__ = "vehicle_positions"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    mission_id: Mapped[str | None] = mapped_column(String, index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    heading: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    mission_id: Mapped[str] = mapped_column(String, index=True)
    waypoints: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    polyline: Mapped[str] = mapped_column(Text)
    distance_km: Mapped[float] = mapped_column(Float)
    duration_min: Mapped[float] = mapped_column(Float)


class Mission(Base):
    __tablename__ = "missions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    reference: Mapped[str] = mapped_column(String, unique=True)
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), index=True)
    driver_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    origin: Mapped[dict[str, Any]] = mapped_column(JSON)
    destination: Mapped[dict[str, Any]] = mapped_column(JSON)
    planned_route_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True, default="planned")
    planned_pickup_at: Mapped[datetime] = mapped_column(UTCDateTime)
    planned_delivery_at: Mapped[datetime] = mapped_column(UTCDateTime)
    actual_pickup_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    departed_pickup_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    actual_delivery_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    current_eta: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # État de dérive calculé par le TMS à chaque position reçue.
    deviation_offset_km: Mapped[float | None] = mapped_column(Float)
    deviation_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    deviation_computed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    route_progress_km: Mapped[float | None] = mapped_column(Float)
    simulation_run_id: Mapped[str | None] = mapped_column(String, index=True)
    scenario: Mapped[str | None] = mapped_column(String)


class MissionEvent(Base):
    __tablename__ = "mission_events"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, unique=True, index=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("missions.id"), index=True)
    type: Mapped[str] = mapped_column(String, index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String)


class SimulationClock(Base):
    """Horloge partagée API/simulateur. Une seule ligne (id=1)."""

    __tablename__ = "simulation_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    real_anchor: Mapped[datetime] = mapped_column(UTCDateTime)
    sim_anchor: Mapped[datetime] = mapped_column(UTCDateTime)
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    run_id: Mapped[str | None] = mapped_column(String)
