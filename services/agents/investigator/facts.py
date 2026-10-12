"""Faits clés, calculés de façon déterministe avant tout appel au LLM (F21).

Deux raisons, et la seconde est la plus importante :

  - **épargner des lectures.** Ce que nous savons calculer, l'enquêteur n'a pas
    à aller le chercher par des appels d'outils — qui coûtent du temps, du
    quota TMS et des jetons à chaque tour ;
  - **lui donner un socle qu'il ne peut pas inventer.** Un LLM à qui l'on
    demande « depuis combien de temps le véhicule est-il arrêté ? » peut
    répondre de travers avec assurance. Si le chiffre est dans son contexte
    initial, la question ne se pose plus.

Ce module ne contient donc aucune heuristique et aucune interprétation : que
des mesures, toutes rapportées à l'heure TMS.
"""

from datetime import datetime
from typing import Any

from shared.mapped_dto import Alert, MissionSnapshot
from shared.utils.serialization import to_jsonable


def _minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def build_observed_facts(alert: Alert, snapshot: MissionSnapshot | None) -> dict[str, Any]:
    """Rassemble les mesures utiles à l'analyse d'une alerte.

    Le snapshot peut manquer — la mission a pu se terminer entre la détection
    et l'enquête. Les faits sont alors réduits à ce que l'alerte porte
    elle-même, et l'absence est signalée plutôt que comblée.
    """
    facts: dict[str, Any] = {
        "alert": {
            "rule_id": alert.rule_id,
            "rule_version": alert.rule_version,
            "severity": alert.severity.value,
            "category": alert.category,
            "detected_at_tms": alert.detected_at.isoformat(),
            # Les valeurs qui ont déclenché la règle : le point de départ de
            # toute vérification, et la raison pour laquelle l'alerte existe.
            "observed_data": to_jsonable(alert.observed_data),
        },
        "mission_snapshot_available": snapshot is not None,
    }

    if snapshot is None:
        facts["note"] = (
            "La mission n'est plus lisible au moment de l'enquête : elle a pu se "
            "terminer depuis la détection. Ne rien conclure de cette absence."
        )
        return facts

    now = snapshot.observed_at
    facts["tms_time"] = now.isoformat()
    facts["mission"] = {
        "reference": snapshot.reference,
        "status": snapshot.status.value,
        "snapshot_complete": snapshot.snapshot_complete,
        "missing_parts": list(snapshot.missing_parts),
        "minutes_since_detection": _minutes_between(alert.detected_at, now),
    }

    last_event = snapshot.last_event
    if last_event is not None:
        facts["last_event"] = {
            "type": last_event.type.value,
            "occurred_at": last_event.occurred_at.isoformat(),
            "age_minutes": _minutes_between(last_event.occurred_at, now),
        }
    facts["event_count"] = len(snapshot.events)

    if snapshot.vehicle is not None:
        vehicle = snapshot.vehicle
        facts["vehicle"] = {
            "plate": vehicle.plate,
            "status": vehicle.status.value,
            "last_seen_age_minutes": _minutes_between(vehicle.last_seen_at, now),
            "speed_kmh": (vehicle.current_position.speed_kmh if vehicle.current_position else None),
        }

    if snapshot.deviation is not None:
        facts["deviation"] = {
            "current_offset_km": snapshot.deviation.current_offset_km,
            "duration_minutes": snapshot.deviation.duration_minutes,
            "severity": (snapshot.deviation.severity.value if snapshot.deviation.severity else None),
        }

    if snapshot.eta is not None:
        facts["eta"] = {
            "delay_minutes": snapshot.eta.delay_minutes,
            "remaining_distance_km": snapshot.eta.remaining_distance_km,
            "planned_delivery_at": (
                snapshot.eta.planned_delivery_at.isoformat() if snapshot.eta.planned_delivery_at else None
            ),
        }

    if snapshot.unloading is not None:
        facts["delivery_stop"] = {
            "label": snapshot.unloading.place.label,
            "planned_at": (
                snapshot.unloading.planned_at.isoformat() if snapshot.unloading.planned_at else None
            ),
        }

    # Le chauffeur est nommé, sans son numéro : l'enquêteur n'a pas à composer
    # lui-même, il demande un appel par l'outil prévu et sous garde-fous.
    if snapshot.driver is not None:
        facts["driver"] = {
            "name": snapshot.driver.name,
            "reachable_by_phone": bool(snapshot.driver.phone),
        }

    return facts
