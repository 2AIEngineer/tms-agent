"""Simulateur : fait vivre des missions dans le TMS (positions toutes les 30 s, événements, anomalies).

Le simulateur écrit directement via la couche de services du TMS (même base que l'API),
comme le ferait le back-office d'un TMS alimenté par les boîtiers télématiques et l'appli chauffeur.
"""

import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from tms import clock
from tms.db import init_db, session_scope
from tms.geo import LatLon, RoutePath, bearing_deg, destination_point, encode_polyline
from tms.ids import new_id
from tms.models import Mission, MissionEvent, Route, Vehicle, VehiclePosition
from tms.seed import seed_reference_data
from tms.services import missions as mission_service
from tms.services import tracking
from tms.simulator.routing import plan_route
from tms.simulator.scenarios import SCENARIOS, Scenario

log = logging.getLogger(__name__)

# Chronologie type d'une mission (minutes).
APPROACH_MIN = 10  # trajet à vide vers le point de chargement
APPROACH_KM = 4.0
LOADING_STARTED_AFTER_MIN = 5
LOADING_COMPLETED_AFTER_MIN = 30
DEPARTED_AFTER_MIN = 32
UNLOADING_STARTED_AFTER_MIN = 5
UNLOADING_COMPLETED_AFTER_MIN = 20
DELIVERY_BUFFER_MIN = 15  # marge du planning
GPS_NOISE_DEG = 0.00012  # ~15 m


@dataclass
class _Scheduled:
    at: datetime
    type: str
    payload: dict = field(default_factory=dict)


class MissionRunner:
    """Machine à états d'une mission : approach → at_pickup → transit → at_delivery → done."""

    def __init__(
        self,
        scenario: Scenario,
        mission_id: str,
        path: RoutePath,
        route_distance_km: float,
        route_duration_min: float,
        t0: datetime,
        rng: random.Random,
    ):
        self.s = scenario
        self.mission_id = mission_id
        self.path = path
        self.t0 = t0
        self.rng = rng
        self.base_speed_kmh = route_distance_km / route_duration_min * 60
        self.km_scale = path.length_km / route_distance_km  # km réels -> km du tracé
        away = (bearing_deg(scenario.origin.point, path.points[1]) + 180) % 360
        self.approach_start = destination_point(scenario.origin.point, away, APPROACH_KM)
        self.phase = "approach"
        self.progress_km = 0.0
        self.last_at = t0
        self.heading = bearing_deg(self.approach_start, scenario.origin.point)
        self.schedule: list[_Scheduled] = []
        self.stop_until: datetime | None = None
        self.incident_fired = False
        self.immobilization_ended = False

    @property
    def done(self) -> bool:
        return self.phase == "done"

    def _elapsed_min(self, at: datetime) -> float:
        return (at - self.t0).total_seconds() / 60

    def _jitter(self, p: LatLon) -> LatLon:
        return (
            p[0] + self.rng.gauss(0, GPS_NOISE_DEG),
            p[1] + self.rng.gauss(0, GPS_NOISE_DEG),
        )

    def _deviation_offset_km(self, elapsed: float) -> float:
        d = self.s.deviation
        if d is None or not (d.start_min <= elapsed <= d.start_min + d.duration_min):
            return 0.0
        u = elapsed - d.start_min
        # Plateau entre ~max-0.5 et ~max+0.5 km (6-8 km pour max=7.5).
        plateau = d.max_offset_km + 0.5 * math.sin(u / 3)
        if u < d.ramp_min:
            return plateau * u / d.ramp_min
        if u > d.duration_min - d.ramp_min:
            return plateau * (d.duration_min - u) / d.ramp_min
        return plateau

    def _immobilized(self, elapsed: float) -> bool:
        i = self.s.immobilization
        return i is not None and i.start_min <= elapsed < i.start_min + i.duration_min

    def _transit_position(self, at: datetime, dt_min: float) -> tuple[LatLon, float]:
        elapsed = self._elapsed_min(at)
        s = self.s

        if s.incident and not self.incident_fired and elapsed >= s.incident.at_min:
            self.incident_fired = True
            self.stop_until = at + timedelta(minutes=s.incident.stop_min)
            self.schedule.append(_Scheduled(at, "driver_message", dict(s.incident.payload)))
            self.schedule.append(
                _Scheduled(
                    self.stop_until,
                    "driver_message",
                    {
                        "severity": "low",
                        "category": "resume",
                        "text": "Reprise de la route après l'incident.",
                    },
                )
            )

        immobilized = self._immobilized(elapsed)
        if (
            s.immobilization
            and not immobilized
            and elapsed >= s.immobilization.start_min
            and not self.immobilization_ended
        ):
            # Fin du gel des événements : le chauffeur se manifeste.
            self.immobilization_ended = True
            self.schedule.append(
                _Scheduled(
                    at,
                    "driver_message",
                    {
                        "severity": "medium",
                        "category": "resume",
                        "text": "Panne réparée, reprise de la route.",
                    },
                )
            )

        stopped = immobilized or (self.stop_until is not None and at < self.stop_until)
        offset = self._deviation_offset_km(elapsed)
        speed = 0.0
        if not stopped:
            speed = (
                self.base_speed_kmh
                * self.rng.uniform(0.9, 1.1)
                * (s.deviation.speed_factor if offset > 0 else 1)
            )
            self.progress_km = min(
                self.path.length_km,
                self.progress_km + speed * dt_min / 60 * self.km_scale,
            )
        point, heading = self.path.point_at(self.progress_km)
        if offset > 0:
            point = destination_point(point, heading + 90, offset)
        if speed > 0:
            self.heading = heading
        return point, speed

    def step(self, session: Session, at: datetime) -> None:
        mission = session.get(Mission, self.mission_id)
        vehicle = session.get(Vehicle, self.s.vehicle_id)
        dt_min = (at - self.last_at).total_seconds() / 60
        self.last_at = at
        elapsed = self._elapsed_min(at)

        if self.phase == "approach":
            frac = min(1.0, elapsed / APPROACH_MIN)
            a, b = self.approach_start, self.s.origin.point
            point = (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)
            speed = APPROACH_KM / APPROACH_MIN * 60 * self.rng.uniform(0.9, 1.1) if frac < 1 else 0.0
            if frac >= 1:
                arrival = self.t0 + timedelta(minutes=APPROACH_MIN)
                self.phase = "at_pickup"
                self.schedule += [
                    _Scheduled(arrival, "arrived_pickup"),
                    _Scheduled(
                        arrival + timedelta(minutes=LOADING_STARTED_AFTER_MIN),
                        "loading_started",
                    ),
                    _Scheduled(
                        arrival + timedelta(minutes=LOADING_COMPLETED_AFTER_MIN),
                        "loading_completed",
                        {"weight_kg": self.s.weight_kg},
                    ),
                    _Scheduled(
                        arrival + timedelta(minutes=DEPARTED_AFTER_MIN),
                        "departed_pickup",
                    ),
                ]
        elif self.phase == "at_pickup":
            point, speed = self.s.origin.point, 0.0
        elif self.phase == "transit":
            point, speed = self._transit_position(at, dt_min)
            if self.progress_km >= self.path.length_km:
                self.phase = "at_delivery"
                self.schedule += [
                    _Scheduled(at, "arrived_delivery"),
                    _Scheduled(
                        at + timedelta(minutes=UNLOADING_STARTED_AFTER_MIN),
                        "unloading_started",
                    ),
                    _Scheduled(
                        at + timedelta(minutes=UNLOADING_COMPLETED_AFTER_MIN),
                        "unloading_completed",
                    ),
                ]
                speed = 0.0
        else:  # at_delivery
            point, speed = self.s.destination.point, 0.0

        lat, lon = self._jitter(point)
        tracking.record_position(
            session,
            vehicle,
            lat=lat,
            lon=lon,
            heading=round(self.heading, 1),
            speed_kmh=round(speed, 1),
            recorded_at=at,
        )
        self._emit_due_events(session, mission, vehicle, at)

    def _emit_due_events(self, session: Session, mission: Mission, vehicle: Vehicle, at: datetime) -> None:
        due = sorted((e for e in self.schedule if e.at <= at), key=lambda e: e.at)
        self.schedule = [e for e in self.schedule if e.at > at]
        for e in due:
            mission_service.add_event(
                session,
                mission,
                type_=e.type,
                occurred_at=e.at,
                source="driver_app",
                lat=vehicle.lat,
                lon=vehicle.lon,
                payload=e.payload,
            )
            if e.type == "departed_pickup":
                self.phase = "transit"
            elif e.type == "unloading_completed":
                self.phase = "done"
                vehicle.status = "idle"


# --- Orchestration ----------------------------------------------------------------------


def reset_data(session: Session) -> None:
    """Supprime missions, événements, positions et itinéraires (conserve utilisateurs et véhicules)."""
    for model in (MissionEvent, VehiclePosition, Route, Mission):
        session.query(model).delete()
    session.execute(
        update(Vehicle).values(
            status="idle",
            lat=None,
            lon=None,
            heading=None,
            speed_kmh=None,
            position_recorded_at=None,
            last_seen_at=None,
        )
    )
    clock.reset_clock(session)


def _cancel_unfinished(session: Session, where, at: datetime) -> list[str]:
    missions = session.scalars(
        select(Mission).where(Mission.status.in_(("planned", "in_progress")), where)
    ).all()
    for m in missions:
        mission_service.set_status(session, m, "cancelled", at)
        session.execute(update(Vehicle).where(Vehicle.id == m.vehicle_id).values(status="idle"))
    return [m.id for m in missions]


def _create_mission(
    session: Session,
    scenario: Scenario,
    t0: datetime,
    run_id: str,
    seq: int,
    routing: str,
    rng: random.Random,
) -> MissionRunner:
    planned = plan_route(scenario.waypoints, routing)
    mission_id, route_id = new_id("msn"), new_id("rte")
    planned_pickup = t0 + timedelta(minutes=APPROACH_MIN)
    planned_delivery = planned_pickup + timedelta(
        minutes=DEPARTED_AFTER_MIN + planned.duration_min + DELIVERY_BUFFER_MIN
    )
    session.add(
        Route(
            id=route_id,
            mission_id=mission_id,
            waypoints=[
                {"lat": round(lat, 6), "lon": round(lon, 6), "sequence": i}
                for i, (lat, lon) in enumerate(planned.points)
            ],
            polyline=encode_polyline(planned.points),
            distance_km=planned.distance_km,
            duration_min=planned.duration_min,
        )
    )
    mission = Mission(
        id=mission_id,
        reference=f"MIS-{t0.year}-{seq:05d}",
        vehicle_id=scenario.vehicle_id,
        driver_id=scenario.driver_id,
        origin={
            "label": scenario.origin.label,
            "lat": scenario.origin.lat,
            "lon": scenario.origin.lon,
        },
        destination={
            "label": scenario.destination.label,
            "lat": scenario.destination.lat,
            "lon": scenario.destination.lon,
        },
        planned_route_id=route_id,
        status="planned",
        planned_pickup_at=planned_pickup,
        planned_delivery_at=planned_delivery,
        current_eta=planned_delivery,
        created_at=t0,
        updated_at=t0,
        simulation_run_id=run_id,
        scenario=scenario.key,
    )
    session.add(mission)
    session.flush()
    # La mission démarre aussitôt : le véhicule part vers le point de chargement.
    mission_service.set_status(session, mission, "in_progress", t0)
    path = RoutePath(planned.points)
    return MissionRunner(scenario, mission_id, path, planned.distance_km, planned.duration_min, t0, rng)


@dataclass
class RunResult:
    run_id: str
    mission_ids: list[str]
    completed: bool
    started_at: datetime
    ended_at: datetime


def run_simulation(
    scenario_keys: list[str],
    *,
    speed: float = 1.0,
    tick_seconds: int = 30,
    routing: str = "static",
    reset: bool = False,
    skip_minutes: float = 0.0,
    fast_forward: bool = False,
    seed: int | None = None,
    on_start: Callable[[str, list[tuple[str, str, Scenario]]], None] | None = None,
    on_tick: Callable[[datetime, list[MissionRunner]], None] | None = None,
) -> RunResult:
    """Crée les missions puis les fait vivre jusqu'à leur terme.

    - `speed` : facteur d'accélération du temps (60 = 1 minute simulée par seconde).
    - `skip_minutes` : les N premières minutes sont simulées instantanément.
    - `fast_forward` : toute la simulation est jouée instantanément (historique complet).
    """
    unknown = [k for k in scenario_keys if k not in SCENARIOS]
    if unknown:
        raise ValueError(f"scénario(s) inconnu(s) : {', '.join(unknown)}")
    scenarios = [SCENARIOS[k] for k in scenario_keys]
    vehicles = [s.vehicle_id for s in scenarios]
    if len(set(vehicles)) != len(vehicles):
        raise ValueError("chaque scénario doit utiliser un véhicule distinct")

    init_db()
    seed_reference_data()
    rng = random.Random(seed)
    run_id = new_id("run")
    tick = timedelta(seconds=tick_seconds)

    with session_scope() as session:
        if reset:
            reset_data(session)
        t0 = clock.now(session)
        cancelled = _cancel_unfinished(session, Mission.vehicle_id.in_(vehicles), t0)
        if cancelled:
            log.info("missions restées actives annulées : %s", ", ".join(cancelled))
        # Pendant le rattrapage (`skip_minutes` / `fast_forward`), l'horloge reste à T0 ;
        # elle n'avance qu'une fois les données écrites, pour que l'API reste cohérente.
        clock.set_clock(session, speed=1.0, running=True, run_id=run_id, anchor=t0)
        seq = session.scalar(select(func.count()).select_from(Mission)) + 1
        runners = [
            _create_mission(session, s, t0, run_id, seq + i, routing, rng) for i, s in enumerate(scenarios)
        ]

    if on_start:
        with session_scope() as session:
            refs = {
                m.id: m.reference
                for m in session.scalars(select(Mission).where(Mission.simulation_run_id == run_id))
            }
        on_start(run_id, [(r.mission_id, refs[r.mission_id], r.s) for r in runners])

    at = t0
    skip_until = t0 + timedelta(minutes=skip_minutes)
    state: clock.ClockState | None = None
    completed = False
    try:
        while not all(r.done for r in runners):
            at += tick
            if not fast_forward and at > skip_until:
                if state is None:
                    with session_scope() as session:
                        state = clock.set_clock(
                            session,
                            speed=speed,
                            running=True,
                            run_id=run_id,
                            anchor=at - tick,
                        )
                wait = (at - state.now()).total_seconds() / speed
                if wait > 0:
                    time.sleep(wait)
            with session_scope() as session:
                for r in runners:
                    if not r.done:
                        r.step(session, at)
            if on_tick:
                on_tick(at, runners)
        completed = True
    finally:
        with session_scope() as session:
            if not completed:
                _cancel_unfinished(session, Mission.simulation_run_id == run_id, at)
            # L'horloge repart à vitesse réelle depuis l'heure simulée atteinte.
            clock.set_clock(session, speed=1.0, running=False, run_id=None, anchor=at)

    return RunResult(run_id, [r.mission_id for r in runners], completed, t0, at)
