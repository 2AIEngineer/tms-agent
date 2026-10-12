"""Événements de mission, dans le vocabulaire canonique de l'orchestrateur."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from .base import Pivot, Position


class EventType(StrEnum):
    """Vocabulaire fermé des événements de mission.

    Fermé volontairement : c'est ce qui permet aux règles d'être écrites une
    seule fois pour tous les TMS. Un type propre à un TMS qui n'entre dans
    aucune de ces catégories est mappé sur `OTHER`, son libellé d'origine étant
    conservé dans `raw_type` — l'information n'est jamais perdue, mais elle
    n'entre pas dans le pivot sans décision explicite.
    """

    ARRIVED_PICKUP = "arrived_pickup"
    LOADING_STARTED = "loading_started"
    LOADING_COMPLETED = "loading_completed"
    DEPARTED_PICKUP = "departed_pickup"
    ARRIVED_DELIVERY = "arrived_delivery"
    UNLOADING_STARTED = "unloading_started"
    UNLOADING_COMPLETED = "unloading_completed"
    DRIVER_MESSAGE = "driver_message"
    DEVIATION_DETECTED = "deviation_detected"
    OTHER = "other"


class EventSource(StrEnum):
    DRIVER_APP = "driver_app"
    TMS = "tms"
    UNKNOWN = "unknown"


class MissionEvent(Pivot):
    """Un événement tel que le TMS l'a enregistré, traduit dans le pivot.

    `occurred_at` est en heure TMS. L'identifiant vient du TMS et sert de clé
    d'idempotence : un événement n'est évalué qu'une fois (F7).
    """

    id: str
    mission_id: str
    type: EventType
    raw_type: str | None = Field(
        default=None,
        description="Type d'origine côté TMS, conservé quand il ne correspond à aucun type canonique.",
    )
    occurred_at: datetime
    position: Position | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: EventSource = EventSource.UNKNOWN

    distance_to_target_km: float | None = Field(
        default=None,
        description="Distance entre la position de l'événement et l'étape attendue. "
        "Champ dérivé, calculé par le monitoring : le TMS ne le fournit pas.",
    )
