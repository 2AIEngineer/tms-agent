"""Scénarios de missions simulées (Document 1 — Simulation du flux temps réel)."""

from dataclasses import dataclass, field

from tms.geo import LatLon


@dataclass(frozen=True)
class Place:
    label: str
    lat: float
    lon: float

    @property
    def point(self) -> LatLon:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class Deviation:
    """Décalage progressif de la position par rapport à l'itinéraire."""

    start_min: float  # minutes après le démarrage de la mission
    duration_min: float
    max_offset_km: float
    ramp_min: float  # durée de la montée (et de la descente) du décalage
    speed_factor: float = 0.6  # progression ralentie pendant le détour


@dataclass(frozen=True)
class Immobilization:
    """Véhicule `stopped` et aucun événement de mission pendant la fenêtre."""

    start_min: float
    duration_min: float


@dataclass(frozen=True)
class Incident:
    """Message chauffeur critique suivi d'un arrêt."""

    at_min: float
    stop_min: float
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    key: str
    description: str
    vehicle_id: str
    driver_id: str
    origin: Place
    destination: Place
    waypoints: tuple[LatLon, ...]  # tracé approximatif (autoroutes), origine et destination incluses
    weight_kg: int
    deviation: Deviation | None = None
    immobilization: Immobilization | None = None
    incident: Incident | None = None


SCENARIOS: dict[str, Scenario] = {
    s.key: s
    for s in [
        Scenario(
            key="nominal",
            description="Mission nominale Casablanca → Rabat, sans anomalie.",
            vehicle_id="veh_201",
            driver_id="drv_77",
            origin=Place("Entrepôt Casablanca", 33.5731, -7.5898),
            destination=Place("Client Rabat", 34.0209, -6.8416),
            waypoints=(
                (33.5731, -7.5898),
                (33.6050, -7.5300),
                (33.6370, -7.4500),
                (33.6860, -7.3830),
                (33.7890, -7.1590),
                (33.8520, -7.0300),
                (33.9280, -6.9060),
                (34.0209, -6.8416),
            ),
            weight_kg=12400,
        ),
        Scenario(
            key="deviation",
            description="Casablanca → Marrakech ; à T+45 min, décalage progressif de 6 à 8 km pendant 30 min.",
            vehicle_id="veh_202",
            driver_id="drv_78",
            origin=Place("Plateforme Casablanca Aïn Sebaâ", 33.6050, -7.5300),
            destination=Place("Client Marrakech", 31.6295, -7.9811),
            waypoints=(
                (33.6050, -7.5300),
                (33.5500, -7.6000),
                (33.4300, -7.6000),
                (33.2650, -7.5870),
                (33.0010, -7.6160),
                (32.7500, -7.6800),
                (32.5500, -7.7500),
                (32.2360, -7.9540),
                (31.9500, -7.9800),
                (31.6295, -7.9811),
            ),
            weight_kg=18200,
            deviation=Deviation(start_min=45, duration_min=30, max_offset_km=7.5, ramp_min=5),
        ),
        Scenario(
            key="immobilization",
            description="Tanger → Fès ; à T+1 h, véhicule stopped et gel des événements pendant 3 h.",
            vehicle_id="veh_203",
            driver_id="drv_79",
            origin=Place("Entrepôt Tanger", 35.7595, -5.8340),
            destination=Place("Client Fès", 34.0331, -5.0003),
            waypoints=(
                (35.7595, -5.8340),
                (35.4500, -5.9800),
                (35.1700, -6.0800),
                (34.9500, -5.9800),
                (34.6200, -5.9500),
                (34.3000, -5.7000),
                (34.1000, -5.4000),
                (34.0331, -5.0003),
            ),
            weight_kg=9800,
            immobilization=Immobilization(start_min=60, duration_min=180),
        ),
        Scenario(
            key="incident",
            description="Casablanca → El Jadida ; à T+55 min, incident critique signalé par le chauffeur (optionnel).",
            vehicle_id="veh_204",
            driver_id="drv_80",
            origin=Place("Entrepôt Casablanca Sud", 33.5400, -7.6500),
            destination=Place("Client El Jadida", 33.2316, -8.5007),
            waypoints=(
                (33.5400, -7.6500),
                (33.5000, -7.7800),
                (33.4300, -7.9500),
                (33.3400, -8.1700),
                (33.2700, -8.3800),
                (33.2316, -8.5007),
            ),
            weight_kg=7600,
            incident=Incident(
                at_min=55,
                stop_min=40,
                payload={
                    "severity": "critical",
                    "category": "accident",
                    "text": "Accrochage avec un véhicule léger, pas de blessé. Attente de la police.",
                },
            ),
        ),
    ]
}

DEFAULT_SCENARIOS = ("nominal", "deviation", "immobilization")
