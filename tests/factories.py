"""Fabriques de snapshots pour les tests.

Un constructeur paramétrable plutôt que des fixtures figées : chaque test dit
seulement ce qui compte pour lui (« écart de 7 km depuis 25 minutes ») et le
reste du snapshot reste réaliste. Les tests deviennent lisibles comme des
scénarios d'exploitation.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from shared.mapped_dto import (
    ActualTimes,
    DeviationSnapshot,
    DriverSnapshot,
    EtaSnapshot,
    EventSource,
    EventType,
    MissionEvent,
    MissionSnapshot,
    MissionStatus,
    Place,
    PlannedTimes,
    Position,
    StopSnapshot,
    VehicleSnapshot,
    VehicleStatus,
)
from tests.fake_tms import TMS_NOW as FAKE_TMS_NOW  # noqa: F401  (cohérence documentée)

# Heure TMS de référence des tests. Fixe et explicite : aucune assertion ne doit
# dépendre de l'horloge réelle de la machine de test.
TMS_NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

# Identifiants partagés avec `tests/fake_tms.py`. Volontairement identiques :
# les deux doubles décrivent la même mission, l'un côté pivot et l'autre côté
# API du TMS. Les laisser diverger rendrait la chaîne de notification intestable,
# puisque la résolution du chauffeur relit la mission dans le TMS (F16).
MISSION_ID = "msn_1"
VEHICLE_ID = "veh_1"
DRIVER_ID = "drv_1"

# Trajet Paris -> Lyon, qui sert de décor à tous les scénarios.
PARIS = Place(label="Entrepôt Paris Nord", lat=48.9014, lon=2.3522)
LYON = Place(label="Plateforme Lyon Sud", lat=45.7578, lon=4.8320)


def make_event(
    *,
    event_id: str = "evt_1",
    mission_id: str = MISSION_ID,
    event_type: EventType = EventType.DEPARTED_PICKUP,
    occurred_at: datetime | None = None,
    position: Position | None = None,
    payload: dict[str, Any] | None = None,
    source: EventSource = EventSource.TMS,
    distance_to_target_km: float | None = None,
) -> MissionEvent:
    return MissionEvent(
        id=event_id,
        mission_id=mission_id,
        type=event_type,
        occurred_at=occurred_at or TMS_NOW - timedelta(minutes=5),
        position=position,
        payload=payload or {},
        source=source,
        distance_to_target_km=distance_to_target_km,
    )


def make_snapshot(
    *,
    tms_id: str = "x_tms",
    mission_id: str = MISSION_ID,
    reference: str = "REF-0001",
    status: MissionStatus = MissionStatus.IN_PROGRESS,
    vehicle_status: VehicleStatus = VehicleStatus.MOVING,
    vehicle_position: Position | None = None,
    deviation_km: float | None = None,
    deviation_minutes: float | None = None,
    delay_minutes: float | None = None,
    events: list[MissionEvent] | None = None,
    last_event_age_minutes: float | None = None,
    observed_at: datetime = TMS_NOW,
    snapshot_complete: bool = True,
    with_deviation: bool = True,
    with_eta: bool = True,
) -> MissionSnapshot:
    """Construit un snapshot réaliste, en ne précisant que ce qui compte.

    `last_event_age_minutes` est un raccourci de scénario : il fabrique un
    événement daté en conséquence, ce qui évite à chaque test de recalculer la
    date de l'événement à la main.
    """
    if events is None and last_event_age_minutes is not None:
        events = [make_event(occurred_at=observed_at - timedelta(minutes=last_event_age_minutes))]

    return MissionSnapshot(
        tms_id=tms_id,
        mission_id=mission_id,
        reference=reference,
        status=status,
        driver=DriverSnapshot(id=DRIVER_ID, name="Awa Traoré", phone="+33600000001"),
        vehicle=VehicleSnapshot(
            id=VEHICLE_ID,
            plate="AB-123-CD",
            status=vehicle_status,
            current_position=vehicle_position or Position(lat=47.0, lon=3.5, recorded_at=observed_at),
            last_seen_at=observed_at,
        ),
        loading=StopSnapshot(place=PARIS, planned_at=observed_at - timedelta(hours=3)),
        unloading=StopSnapshot(place=LYON, planned_at=observed_at + timedelta(hours=2)),
        planned_times=PlannedTimes(
            pickup_at=observed_at - timedelta(hours=3),
            delivery_at=observed_at + timedelta(hours=2),
        ),
        actual_times=ActualTimes(pickup_at=observed_at - timedelta(hours=2, minutes=50)),
        deviation=(
            DeviationSnapshot(
                current_offset_km=deviation_km,
                current_offset_since=(
                    observed_at - timedelta(minutes=deviation_minutes) if deviation_minutes else None
                ),
                duration_minutes=deviation_minutes,
                vehicle_position=vehicle_position or Position(lat=47.0, lon=3.5),
            )
            if with_deviation
            else None
        ),
        eta=(
            EtaSnapshot(
                planned_delivery_at=observed_at + timedelta(hours=2),
                current_eta=(
                    observed_at + timedelta(hours=2, minutes=delay_minutes)
                    if delay_minutes is not None
                    else None
                ),
                delay_minutes=delay_minutes,
            )
            if with_eta
            else None
        ),
        events=events or [],
        observed_at=observed_at,
        snapshot_complete=snapshot_complete,
    )
