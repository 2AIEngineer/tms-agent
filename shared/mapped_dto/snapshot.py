"""Modèle canonique d'une mission : le contrat pivot de l'orchestrateur.

Aucun service ne lit le JSON brut du TMS. Le connecteur traduit le modèle
propre à chaque TMS vers ces objets, et tout le reste du système — moteur de
règles, alertes, enquêteur, assistant — ne connaît que ceux-ci. Brancher un
nouveau TMS revient donc à écrire un mapper, sans toucher au métier.

    JSON TMS  ->  TmsConnector  ->  MissionSnapshot  ->  monitoring
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from .base import Pivot, Place, Position
from .events import MissionEvent


class MissionStatus(StrEnum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class VehicleStatus(StrEnum):
    IDLE = "idle"
    MOVING = "moving"
    STOPPED = "stopped"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class DeviationSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DriverSnapshot(Pivot):
    id: str
    name: str
    phone: str | None = None
    email: str | None = None
    role: str = "driver"
    management_level: str = "M+0"


class VehicleSnapshot(Pivot):
    id: str
    plate: str
    status: VehicleStatus = VehicleStatus.UNKNOWN
    current_position: Position | None = None
    last_seen_at: datetime | None = None


class StopSnapshot(Pivot):
    """Étape de la mission : chargement ou déchargement."""

    place: Place
    planned_at: datetime | None = None
    actual_at: datetime | None = None


class RouteSnapshot(Pivot):
    """Itinéraire planifié, servant de référence au calcul d'écart."""

    waypoints: list[Position] = Field(default_factory=list)
    distance_km: float | None = None
    duration_minutes: float | None = None


class DeviationSnapshot(Pivot):
    """Écart courant entre le véhicule et son itinéraire planifié.

    Les champs sont nullables : tant que le véhicule n'a pas quitté le point de
    chargement, l'écart n'a pas de sens. Une règle portant sur un champ absent
    est fausse, jamais en erreur.
    """

    current_offset_km: float | None = None
    current_offset_since: datetime | None = None
    duration_minutes: float | None = None
    severity: DeviationSeverity | None = None
    vehicle_position: Position | None = None
    computed_at: datetime | None = None


class EtaSnapshot(Pivot):
    """Heure d'arrivée estimée et retard associé."""

    planned_delivery_at: datetime | None = None
    current_eta: datetime | None = None
    delay_minutes: float | None = Field(
        default=None,
        description="current_eta - planned_delivery_at, en minutes. Négatif = avance.",
    )
    remaining_distance_km: float | None = None
    computed_at: datetime | None = None


class PlannedTimes(Pivot):
    pickup_at: datetime | None = None
    delivery_at: datetime | None = None


class ActualTimes(Pivot):
    pickup_at: datetime | None = None
    delivery_at: datetime | None = None


class MissionSnapshot(Pivot):
    """État d'une mission à un instant donné, dans le modèle de l'orchestrateur.

    `observed_at` et toutes les dates portées par ce snapshot sont en **heure
    TMS** et non en heure locale : le simulateur peut accélérer son horloge ou
    la remettre à zéro, et les règles temporelles doivent le suivre (F3).

    `snapshot_complete` distingue une vue complète d'une vue partielle. C'est
    un garde-fou de correction, pas une statistique : une alerte d'état ne peut
    être close automatiquement que sur un snapshot complet, sinon un appel TMS
    en échec se traduirait par un faux « résolu » (F14).
    """

    # Provenance de la mission. Obligatoire, et non optionnelle avec un repli :
    # deux TMS peuvent porter une mission du même identifiant, et c'est ce champ
    # qui empêche alors leurs alertes de se confondre. Un défaut ici ne se
    # verrait qu'au moment où une alerte attendue disparaît.
    tms_id: str = Field(min_length=1)
    mission_id: str
    reference: str
    status: MissionStatus

    driver: DriverSnapshot | None = None
    vehicle: VehicleSnapshot | None = None

    loading: StopSnapshot | None = None
    unloading: StopSnapshot | None = None

    planned_route: RouteSnapshot | None = None
    current_position: Position | None = None

    planned_times: PlannedTimes = Field(default_factory=PlannedTimes)
    actual_times: ActualTimes = Field(default_factory=ActualTimes)

    deviation: DeviationSnapshot | None = None
    eta: EtaSnapshot | None = None

    events: list[MissionEvent] = Field(default_factory=list)

    observed_at: datetime
    snapshot_complete: bool = True
    missing_parts: tuple[str, ...] = Field(
        default=(),
        description="Parties du snapshot que le TMS n'a pas rendues (ex. 'deviation'). "
        "Reportées dans le bilan de cycle sans interrompre celui-ci (F4).",
    )

    @property
    def key(self) -> tuple[str, str]:
        """Identité globale de la mission : sa provenance et son identifiant.

        `mission_id` seul n'est pas une clé dès qu'un second TMS est raccordé.
        """
        return (self.tms_id, self.mission_id)

    @property
    def last_event(self) -> MissionEvent | None:
        """Événement le plus récent, en heure TMS."""
        if not self.events:
            return None
        return max(self.events, key=lambda e: e.occurred_at)
