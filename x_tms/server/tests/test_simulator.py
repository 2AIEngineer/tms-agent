from datetime import timedelta

from sqlalchemy import select
from tms.clock import now
from tms.db import session_scope
from tms.models import Mission, MissionEvent, Vehicle
from tms.services import tracking
from tms.simulator.engine import run_simulation

LIFECYCLE = [
    "arrived_pickup",
    "loading_started",
    "loading_completed",
    "departed_pickup",
    "arrived_delivery",
    "unloading_started",
    "unloading_completed",
]


def _probe(times_min, keys=("nominal", "deviation", "immobilization")):
    """Joue la simulation et capture des observations à des instants donnés (minutes après T0)."""
    observations: dict[float, dict] = {}
    start: dict = {}

    def on_start(run_id, missions):
        with session_scope() as s:
            start["t0"] = s.get(Mission, missions[0][0]).created_at
            start["ids"] = {sc.key: mid for mid, _, sc in missions}

    def on_tick(at, runners):
        minute = (at - start["t0"]).total_seconds() / 60
        if minute not in times_min:
            return
        snap = {}
        with session_scope() as s:
            for key, mid in start["ids"].items():
                m = s.get(Mission, mid)
                v = s.get(Vehicle, m.vehicle_id)
                last_evt = s.scalars(
                    select(MissionEvent)
                    .where(MissionEvent.mission_id == mid)
                    .order_by(MissionEvent.occurred_at.desc())
                ).first()
                snap[key] = {
                    "deviation": tracking.deviation_for(s, m, v),
                    "eta": tracking.eta_for(s, m, at),
                    "vehicle_status": v.status,
                    "last_event_age": (at - last_evt.occurred_at).total_seconds() / 60 if last_evt else None,
                }
        observations[minute] = snap

    result = run_simulation(list(keys), fast_forward=True, seed=7, on_start=on_start, on_tick=on_tick)
    return result, start["ids"], observations


def test_full_lifecycle_and_anomalies():
    result, ids, obs = _probe({30.0, 70.0, 200.0})
    assert result.completed

    with session_scope() as s:
        for mid in ids.values():
            m = s.get(Mission, mid)
            assert m.status == "completed"
            assert m.actual_pickup_at is not None and m.actual_delivery_at is not None
            types = [
                e.type
                for e in s.scalars(
                    select(MissionEvent)
                    .where(MissionEvent.mission_id == mid)
                    .order_by(MissionEvent.occurred_at, MissionEvent.pk)
                )
            ]
            assert [t for t in types if t != "driver_message"] == LIFECYCLE
        assert all(
            v.status == "idle"
            for v in s.scalars(select(Vehicle).where(Vehicle.id.in_(["veh_201", "veh_202", "veh_203"])))
        )
        # L'horloge du TMS a avancé jusqu'à la fin de la simulation.
        assert now(s) >= result.ended_at

    # Avant le départ, pas d'écart calculé.
    assert obs[30.0]["nominal"]["deviation"].current_offset_km is None

    # Mission 2 : dérive > 5 km depuis plus de 20 min vers T+70 (règle deviation_persistent).
    dev = obs[70.0]["deviation"]["deviation"]
    assert 6 <= dev.current_offset_km <= 8.5
    assert dev.duration_minutes > 20
    assert dev.severity == "medium"
    assert obs[70.0]["nominal"]["deviation"].current_offset_km < 0.5

    # Mission 3 : immobilisée, aucun événement depuis > 2 h, retard ETA > 45 min.
    imm = obs[200.0]["immobilization"]
    assert imm["vehicle_status"] == "stopped"
    assert imm["last_event_age"] > 120
    assert imm["eta"].delay_minutes > 45
    assert obs[200.0]["nominal"]["eta"].delay_minutes <= 0


def test_relaunch_creates_new_missions_and_reset(client):
    first = run_simulation(["nominal"], fast_forward=True, seed=1)
    second = run_simulation(["nominal"], fast_forward=True, seed=1)
    assert set(first.mission_ids).isdisjoint(second.mission_ids)
    # Les missions s'enchaînent dans le temps simulé.
    assert second.started_at >= first.ended_at
    assert client.get("/v1/missions").json()["pagination"]["has_more"] is False
    assert len(client.get("/v1/missions").json()["data"]) == 2

    third = run_simulation(["nominal"], fast_forward=True, seed=1, reset=True)
    data = client.get("/v1/missions").json()["data"]
    assert [m["id"] for m in data] == third.mission_ids


def test_incident_scenario_emits_critical_driver_message():
    result = run_simulation(["incident"], fast_forward=True, seed=3)
    with session_scope() as s:
        messages = s.scalars(
            select(MissionEvent).where(
                MissionEvent.mission_id == result.mission_ids[0], MissionEvent.type == "driver_message"
            )
        ).all()
    assert [m.payload["severity"] for m in messages] == ["critical", "low"]
    assert messages[1].occurred_at - messages[0].occurred_at == timedelta(minutes=40)
