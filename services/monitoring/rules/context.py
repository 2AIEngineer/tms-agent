"""Construction du contexte d'évaluation à partir d'un `MissionSnapshot`.

C'est l'étape « champs dérivés » du cycle de surveillance, et la **surface
exposée à l'équipe métier** : les chemins listés ici sont exactement ceux
qu'une règle peut écrire. Elle est construite explicitement plutôt qu'en
sérialisant les DTO, pour trois raisons :

  - les noms diffèrent volontairement (`mission.id` en règle, `mission_id` en
    DTO) : le vocabulaire métier ne doit pas suivre un renommage technique ;
  - certains champs n'existent pas dans le TMS et sont calculés ici
    (`mission.last_event_age_minutes`) ;
  - un `model_dump()` exposerait tout le pivot, y compris ce qui n'a pas de
    sens dans une règle, et transformerait chaque refactorisation interne en
    rupture de contrat métier.
"""

from datetime import datetime
from typing import Any

from shared.mapped_dto import MissionEvent, MissionSnapshot, Position


def _position(position: Position | None) -> dict[str, Any] | None:
    if position is None:
        return None
    return {
        "lat": position.lat,
        "lon": position.lon,
        "heading": position.heading,
        "speed_kmh": position.speed_kmh,
        "recorded_at": position.recorded_at,
    }


def _minutes_since(moment: datetime | None, now: datetime) -> float | None:
    if moment is None:
        return None
    return round((now - moment).total_seconds() / 60.0, 2)


def build_state_context(snapshot: MissionSnapshot) -> dict[str, Any]:
    """Contexte des règles d'état, évalué à chaque cycle sur chaque mission.

    L'heure de référence est `snapshot.observed_at`, c'est-à-dire l'heure TMS
    au moment de l'observation. Elle n'est pas paramétrable : passer une
    horloge locale ici suffirait à fausser toutes les règles temporelles (F3).
    """
    now = snapshot.observed_at
    last_event = snapshot.last_event

    mission: dict[str, Any] = {
        "id": snapshot.mission_id,
        "reference": snapshot.reference,
        "status": snapshot.status,
        "planned_pickup_at": snapshot.planned_times.pickup_at,
        "planned_delivery_at": snapshot.planned_times.delivery_at,
        "actual_pickup_at": snapshot.actual_times.pickup_at,
        "actual_delivery_at": snapshot.actual_times.delivery_at,
        "snapshot_complete": snapshot.snapshot_complete,
        "event_count": len(snapshot.events),
        # Champs dérivés : le TMS ne les fournit pas, le monitoring les calcule.
        "last_event_type": last_event.type if last_event else None,
        "last_event_at": last_event.occurred_at if last_event else None,
        "last_event_age_minutes": _minutes_since(last_event.occurred_at if last_event else None, now),
    }

    context: dict[str, Any] = {
        "mission": mission,
        "tms_time": now,
        # Permet à une règle de ne viser qu'un TMS : un client peut avoir un
        # seuil de retard différent d'un autre, sans dupliquer le fichier de
        # règle ni introduire une notion de client dans le moteur.
        "tms": {"id": snapshot.tms_id},
    }

    if snapshot.vehicle is not None:
        context["vehicle"] = {
            "id": snapshot.vehicle.id,
            "plate": snapshot.vehicle.plate,
            "status": snapshot.vehicle.status,
            "current_position": _position(snapshot.vehicle.current_position),
            "last_seen_at": snapshot.vehicle.last_seen_at,
            "last_seen_age_minutes": _minutes_since(snapshot.vehicle.last_seen_at, now),
        }

    if snapshot.driver is not None:
        context["driver"] = {
            "id": snapshot.driver.id,
            "name": snapshot.driver.name,
            "phone": snapshot.driver.phone,
            "email": snapshot.driver.email,
        }

    if snapshot.deviation is not None:
        context["deviation"] = {
            "current_offset_km": snapshot.deviation.current_offset_km,
            "current_offset_since": snapshot.deviation.current_offset_since,
            "duration_minutes": snapshot.deviation.duration_minutes,
            "severity": snapshot.deviation.severity,
            "vehicle_position": _position(snapshot.deviation.vehicle_position),
        }

    if snapshot.eta is not None:
        context["eta"] = {
            "planned_delivery_at": snapshot.eta.planned_delivery_at,
            "current_eta": snapshot.eta.current_eta,
            "delay_minutes": snapshot.eta.delay_minutes,
            "remaining_distance_km": snapshot.eta.remaining_distance_km,
        }

    for name, stop in (("loading", snapshot.loading), ("unloading", snapshot.unloading)):
        if stop is not None:
            context[name] = {
                "label": stop.place.label,
                "lat": stop.place.lat,
                "lon": stop.place.lon,
                "planned_at": stop.planned_at,
                "actual_at": stop.actual_at,
            }

    if snapshot.planned_route is not None:
        context["route"] = {
            "distance_km": snapshot.planned_route.distance_km,
            "duration_minutes": snapshot.planned_route.duration_minutes,
            "waypoint_count": len(snapshot.planned_route.waypoints),
        }

    return context


def build_event_context(snapshot: MissionSnapshot, event: MissionEvent) -> dict[str, Any]:
    """Contexte d'une règle d'événement : l'état de la mission, plus l'événement.

    L'état complet est conservé pour qu'une règle d'événement puisse se
    restreindre au contexte de sa mission (« incident critique *alors que* la
    mission est en cours »), sans dupliquer la donnée dans l'événement.
    """
    context = build_state_context(snapshot)
    context["event"] = {
        "id": event.id,
        "type": event.type,
        "raw_type": event.raw_type,
        "occurred_at": event.occurred_at,
        "age_minutes": _minutes_since(event.occurred_at, snapshot.observed_at),
        "position": _position(event.position),
        "source": event.source,
        "payload": event.payload,
        "distance_to_target_km": event.distance_to_target_km,
    }
    return context
