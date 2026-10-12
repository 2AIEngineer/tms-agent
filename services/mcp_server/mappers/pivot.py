"""Traduction du modèle d'un TMS vers le contrat pivot, pilotée par son dialecte.

Aucun nom de champ du TMS n'apparaît dans ce fichier : il lit les
correspondances déclarées dans le dialecte. Un TMS qui appelle sa mission
`mss_id` et son horodatage `ts` se traduit donc sans toucher à ce code.

Trois responsabilités qui n'appartiennent qu'ici :

  - **absorber les irrégularités.** Les DTO pivot sont stricts par choix ; c'est
    au mapper d'accepter un champ absent, nul ou d'un type inattendu sans faire
    tomber le snapshot d'une mission entière (F4) ;
  - **ne jamais perdre un événement.** Un type inconnu devient `OTHER` avec son
    libellé d'origine : rejeter l'événement aveuglerait la surveillance sur un
    TMS légèrement différent ;
  - **calculer les champs dérivés** que le TMS ne fournit pas — en premier lieu
    `distance_to_target_km`, dont dépend la règle d'arrivée hors zone. Un calcul
    ne se déclare pas en configuration : il reste du code.
"""

import logging
from datetime import datetime
from typing import Any

from shared.mapped_dto import (
    ActualTimes,
    DeviationSeverity,
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
    RouteSnapshot,
    StopSnapshot,
    VehicleSnapshot,
    VehicleStatus,
)
from shared.utils.geo import haversine_km
from shared.utils.serialization import parse_datetime

from ..connectors.dialect import Entity, TmsDialect
from ..connectors.paths import extract

logger = logging.getLogger(__name__)

# Étape attendue selon le type d'événement : c'est elle qui donne un sens à
# « hors zone ». Une arrivée au chargement se juge par rapport à l'origine, une
# arrivée à la livraison par rapport à la destination.
PICKUP_EVENTS = frozenset(
    {
        EventType.ARRIVED_PICKUP,
        EventType.LOADING_STARTED,
        EventType.LOADING_COMPLETED,
        EventType.DEPARTED_PICKUP,
    }
)
DELIVERY_EVENTS = frozenset(
    {
        EventType.ARRIVED_DELIVERY,
        EventType.UNLOADING_STARTED,
        EventType.UNLOADING_COMPLETED,
    }
)


def as_float(value: Any) -> float | None:
    """Convertit en nombre, sans accepter les booléens (`True` vaudrait 1.0)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


class PivotMapper:
    """Traduit les payloads d'un TMS donné vers les contrats pivot."""

    def __init__(self, dialect: TmsDialect, *, tms_id: str | None = None) -> None:
        self._dialect = dialect
        # La provenance est celle de la **connexion**, pas du dialecte. Deux
        # clients peuvent exploiter le même logiciel de TMS : ils partagent
        # alors un dialecte, mais pas une identité. Dériver la provenance du
        # dialecte étiquetterait leurs missions de la même façon, et la
        # déduplication les confondrait de nouveau — précisément le défaut que
        # la dimension `tms_id` corrige.
        self._tms_id = tms_id or dialect.tms_id

    @property
    def dialect(self) -> TmsDialect:
        return self._dialect

    @property
    def tms_id(self) -> str:
        return self._tms_id

    # --- Lecture élémentaire --------------------------------------------------

    def field(self, entity: Entity, our_path: str, payload: Any) -> Any:
        """Lit un champ par son nom **chez nous**, via la correspondance déclarée.

        Rend `None` si le dialecte ne déclare aucune correspondance : un champ
        que ce TMS ne sait pas fournir est traité comme absent, exactement
        comme un champ vide. La conséquence est documentée par le rapport de
        capacité, et non découverte à l'exécution.
        """
        their_path = self._dialect.their_path(entity, our_path)
        if their_path is None:
            return None
        return extract(payload, their_path)

    def items(self, list_name: str, payload: Any) -> list[Any]:
        """Éléments d'une liste déclarée (`mission.events`, `route.waypoints`)."""
        spec = self._dialect.list_spec(list_name)
        if spec is None:
            return []
        found = extract(payload, spec.path)
        return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []

    # --- Géométrie ------------------------------------------------------------

    def position(self, raw: Any) -> Position | None:
        """Traduit un objet position déjà extrait. `None` si inexploitable."""
        if not isinstance(raw, dict):
            return None
        lat = as_float(self.field(Entity.POSITION, "lat", raw))
        lon = as_float(self.field(Entity.POSITION, "lon", raw))
        if lat is None or lon is None:
            return None
        # Hors bornes : la validation du pivot refuserait l'objet. Mieux vaut une
        # position absente qu'un snapshot entier perdu pour une coordonnée aberrante.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return Position(
            lat=lat,
            lon=lon,
            heading=as_float(self.field(Entity.POSITION, "heading", raw)),
            speed_kmh=as_float(self.field(Entity.POSITION, "speed_kmh", raw)),
            recorded_at=parse_datetime(self.field(Entity.POSITION, "recorded_at", raw)),
        )

    def position_of(self, entity: Entity, our_field: str, payload: Any) -> Position | None:
        """Suit le pointeur vers un objet position, puis le traduit.

        Deux niveaux délibérés : *où* se trouve la position (propre à l'entité
        porteuse) et *comment* elle est décrite (commun à tout le TMS). Un TMS
        qui range la position du véhicule sous `pos` et celle de l'écart sous
        `last_known` n'a qu'un pointeur à déclarer pour chacune.
        """
        return self.position(self.field(entity, our_field, payload))

    def place(self, payload: Any, prefix: str, *, default_label: str) -> Place | None:
        """Traduit un lieu nommé (origine, destination) depuis la mission."""
        if not isinstance(payload, dict):
            return None
        lat = as_float(self.field(Entity.MISSION, f"{prefix}.lat", payload))
        lon = as_float(self.field(Entity.MISSION, f"{prefix}.lon", payload))
        if lat is None or lon is None:
            return None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        label = self.field(Entity.MISSION, f"{prefix}.label", payload)
        return Place(label=str(label) if label else default_label, lat=lat, lon=lon)

    # --- Énumérations ---------------------------------------------------------

    def mission_status(self, raw: Any) -> MissionStatus | None:
        """Traduit le statut de mission.

        Rend `None` sur un statut inconnu, et l'appelant écarte alors la
        mission : contrairement à un type d'événement, un statut non reconnu ne
        peut pas être rangé dans un fourre-tout. Toutes les règles d'état le
        testent, et le deviner conduirait à surveiller — ou à ignorer — une
        mission à tort.
        """
        canonical = self._dialect.canonical("mission_status", raw)
        if canonical is None:
            logger.warning("Statut de mission non reconnu par le dialecte %s : %r", self.tms_id, raw)
            return None
        return MissionStatus(canonical)

    def vehicle_status(self, raw: Any) -> VehicleStatus:
        """Traduit le statut véhicule, avec repli sur `UNKNOWN`.

        Un repli est ici acceptable : les règles comparent ce statut à des
        valeurs précises (`moving`, `stopped`), et `unknown` les rend fausses.
        """
        canonical = self._dialect.canonical("vehicle_status", raw)
        if canonical is None:
            if raw is not None:
                logger.info("Statut véhicule non reconnu : %r", raw)
            return VehicleStatus.UNKNOWN
        return VehicleStatus(canonical)

    def event_type(self, raw: Any) -> tuple[EventType, str | None]:
        """Traduit un type d'événement, sans jamais échouer.

        Rend le type canonique et, s'il a fallu se replier sur `OTHER`, le
        libellé d'origine à conserver dans `raw_type` : l'information n'est
        jamais perdue, mais elle n'entre pas dans le pivot sans décision.
        """
        canonical = self._dialect.canonical("event_type", raw)
        if canonical is not None and canonical != EventType.OTHER.value:
            return EventType(canonical), None
        if isinstance(raw, str) and raw.strip():
            logger.info("Type d'événement non traduit par le dialecte %s : %r", self.tms_id, raw)
            return EventType.OTHER, raw
        return EventType.OTHER, None

    def event_source(self, raw: Any) -> EventSource:
        canonical = self._dialect.canonical("event_source", raw)
        return EventSource(canonical) if canonical else EventSource.UNKNOWN

    def deviation_severity(self, raw: Any) -> DeviationSeverity | None:
        canonical = self._dialect.canonical("deviation_severity", raw)
        return DeviationSeverity(canonical) if canonical else None

    # --- Champs dérivés -------------------------------------------------------

    @staticmethod
    def expected_place(
        event_type: EventType,
        origin: Place | None,
        destination: Place | None,
    ) -> Place | None:
        """Étape de référence d'un événement, s'il en a une.

        Un message chauffeur ou une dérive n'ont pas de point attendu : `None`
        est la bonne réponse, et la distance dérivée restera absente.
        """
        if event_type in PICKUP_EVENTS:
            return origin
        if event_type in DELIVERY_EVENTS:
            return destination
        return None

    @staticmethod
    def distance_to_target_km(position: Position | None, target: Place | None) -> float | None:
        """Distance entre l'événement et l'étape attendue, en kilomètres.

        Champ dérivé : aucun TMS ne le fournit, et aucune correspondance de
        champs ne pourrait l'exprimer. Absent si l'une des deux positions
        manque — la règle qui s'en sert sera alors fausse, plutôt que de
        déclencher sur une valeur inventée.
        """
        if position is None or target is None:
            return None
        return round(haversine_km(position.lat, position.lon, target.lat, target.lon), 3)

    # --- Objets ---------------------------------------------------------------

    def event(
        self,
        raw: dict[str, Any],
        *,
        mission_id: str | None = None,
        origin: Place | None = None,
        destination: Place | None = None,
    ) -> MissionEvent | None:
        """Traduit un événement brut. Rend `None` s'il est inexploitable.

        Un événement sans identifiant ou sans date est écarté : l'identifiant
        porte l'idempotence (F7) et la date porte toute l'évaluation
        temporelle. Sans eux, l'événement serait réévalué sans fin ou daté de
        l'instant présent.
        """
        event_id = self.field(Entity.EVENT, "id", raw)
        occurred_at = parse_datetime(self.field(Entity.EVENT, "occurred_at", raw))
        resolved_mission = self.field(Entity.EVENT, "mission_id", raw) or mission_id

        if not event_id or occurred_at is None or not resolved_mission:
            logger.warning(
                "Événement écarté (identifiant, date ou mission manquants) : %r",
                {key: raw.get(key) for key in list(raw)[:4]},
            )
            return None

        event_type, raw_type = self.event_type(self.field(Entity.EVENT, "type", raw))
        position = self.position_of(Entity.EVENT, "position", raw)
        payload = self.field(Entity.EVENT, "payload", raw)

        return MissionEvent(
            id=str(event_id),
            mission_id=str(resolved_mission),
            type=event_type,
            raw_type=raw_type,
            occurred_at=occurred_at,
            position=position,
            payload=payload if isinstance(payload, dict) else {},
            source=self.event_source(self.field(Entity.EVENT, "source", raw)),
            distance_to_target_km=self.distance_to_target_km(
                position, self.expected_place(event_type, origin, destination)
            ),
        )

    def events(
        self,
        raws: list[dict[str, Any]],
        *,
        mission_id: str | None = None,
        origin: Place | None = None,
        destination: Place | None = None,
    ) -> list[MissionEvent]:
        """Traduit une liste d'événements, en écartant les invalides.

        Les événements valides passent même si certains de leurs voisins sont
        rejetés : perdre toute la chronologie d'une mission à cause d'une ligne
        corrompue serait pire que d'en perdre une.
        """
        mapped = [
            self.event(raw, mission_id=mission_id, origin=origin, destination=destination)
            for raw in raws
            if isinstance(raw, dict)
        ]
        return [event for event in mapped if event is not None]

    def vehicle(self, raw: dict[str, Any] | None) -> VehicleSnapshot | None:
        if not isinstance(raw, dict):
            return None
        vehicle_id = self.field(Entity.VEHICLE, "id", raw)
        if not vehicle_id:
            return None
        plate = self.field(Entity.VEHICLE, "plate", raw)
        return VehicleSnapshot(
            id=str(vehicle_id),
            # L'immatriculation apparaît dans les messages d'alerte : un repli
            # explicite vaut mieux qu'un snapshot refusé par la validation.
            plate=str(plate) if plate else str(vehicle_id),
            status=self.vehicle_status(self.field(Entity.VEHICLE, "status", raw)),
            current_position=self.position_of(Entity.VEHICLE, "current_position", raw),
            last_seen_at=parse_datetime(self.field(Entity.VEHICLE, "last_seen_at", raw)),
        )

    def driver(self, raw: dict[str, Any] | None) -> DriverSnapshot | None:
        return self._person(Entity.DRIVER, raw, default_role="driver")

    def user(self, raw: dict[str, Any] | None) -> DriverSnapshot | None:
        """Un utilisateur de l'annuaire, dans la même forme qu'un chauffeur."""
        return self._person(Entity.USER, raw, default_role="unknown")

    def _person(
        self,
        entity: Entity,
        raw: dict[str, Any] | None,
        *,
        default_role: str,
    ) -> DriverSnapshot | None:
        if not isinstance(raw, dict):
            return None
        person_id = self.field(entity, "id", raw)
        if not person_id:
            return None
        name = self.field(entity, "name", raw)
        return DriverSnapshot(
            id=str(person_id),
            name=str(name) if name else str(person_id),
            phone=_optional_str(self.field(entity, "phone", raw)),
            email=_optional_str(self.field(entity, "email", raw)),
            role=str(self.field(entity, "role", raw) or default_role),
            management_level=str(self.field(entity, "management_level", raw) or "M+0"),
        )

    def deviation(self, raw: dict[str, Any] | None) -> DeviationSnapshot | None:
        """Traduit l'écart courant à l'itinéraire.

        Tous les champs sont nullables : tant que le véhicule n'a pas quitté le
        point de chargement, l'écart n'a pas de sens. Un objet aux champs vides
        est donc un résultat normal, et non une donnée manquante.
        """
        if not isinstance(raw, dict):
            return None
        return DeviationSnapshot(
            current_offset_km=as_float(self.field(Entity.DEVIATION, "current_offset_km", raw)),
            current_offset_since=parse_datetime(self.field(Entity.DEVIATION, "current_offset_since", raw)),
            duration_minutes=as_float(self.field(Entity.DEVIATION, "duration_minutes", raw)),
            severity=self.deviation_severity(self.field(Entity.DEVIATION, "severity", raw)),
            vehicle_position=self.position_of(Entity.DEVIATION, "vehicle_position", raw),
            computed_at=parse_datetime(self.field(Entity.DEVIATION, "computed_at", raw)),
        )

    def eta(self, raw: dict[str, Any] | None) -> EtaSnapshot | None:
        if not isinstance(raw, dict):
            return None
        return EtaSnapshot(
            planned_delivery_at=parse_datetime(self.field(Entity.ETA, "planned_delivery_at", raw)),
            current_eta=parse_datetime(self.field(Entity.ETA, "current_eta", raw)),
            delay_minutes=as_float(self.field(Entity.ETA, "delay_minutes", raw)),
            remaining_distance_km=as_float(self.field(Entity.ETA, "remaining_distance_km", raw)),
            computed_at=parse_datetime(self.field(Entity.ETA, "computed_at", raw)),
        )

    def route(self, raw: dict[str, Any] | None) -> RouteSnapshot | None:
        """Traduit l'itinéraire planifié.

        Une polyligne encodée, si le TMS en fournit une, est ignorée : seuls les
        points de passage servent au pivot.
        """
        if not isinstance(raw, dict):
            return None
        waypoints = [
            position
            for position in (self.position(item) for item in self.items("route.waypoints", raw))
            if position is not None
        ]
        return RouteSnapshot(
            waypoints=waypoints,
            distance_km=as_float(self.field(Entity.ROUTE, "distance_km", raw)),
            duration_minutes=as_float(self.field(Entity.ROUTE, "duration_minutes", raw)),
        )

    def mission_places(self, mission: dict[str, Any]) -> tuple[Place | None, Place | None]:
        """Origine et destination, pour le calcul des distances dérivées."""
        return (
            self.place(mission, "origin", default_label="Chargement"),
            self.place(mission, "destination", default_label="Livraison"),
        )

    def stops(self, mission: dict[str, Any]) -> tuple[StopSnapshot | None, StopSnapshot | None]:
        """Construit les étapes de chargement et de déchargement.

        Le TMS décrit des lieux et des dates séparés ; le pivot les réunit,
        parce qu'une étape est ce dont parlent les règles et les messages.
        """
        origin, destination = self.mission_places(mission)
        loading = (
            StopSnapshot(
                place=origin,
                planned_at=parse_datetime(self.field(Entity.MISSION, "planned_pickup_at", mission)),
                actual_at=parse_datetime(self.field(Entity.MISSION, "actual_pickup_at", mission)),
            )
            if origin
            else None
        )
        unloading = (
            StopSnapshot(
                place=destination,
                planned_at=parse_datetime(self.field(Entity.MISSION, "planned_delivery_at", mission)),
                actual_at=parse_datetime(self.field(Entity.MISSION, "actual_delivery_at", mission)),
            )
            if destination
            else None
        )
        return loading, unloading

    def embedded_events(self, mission: dict[str, Any]) -> list[dict[str, Any]] | None:
        """Chronologie incluse dans le détail de mission, si le TMS en met une.

        S'en servir épargne un appel par mission et par cycle, ce qui compte
        face au quota du TMS. Rend `None` quand la liste n'est pas déclarée ou
        absente, auquel cas l'appelant lira la ressource dédiée.
        """
        spec = self._dialect.list_spec("mission.events")
        if spec is None:
            return None
        found = extract(mission, spec.path)
        return found if isinstance(found, list) else None

    def mission_snapshot(
        self,
        mission: dict[str, Any],
        *,
        observed_at: datetime,
        events: list[dict[str, Any]] | None = None,
        vehicle: dict[str, Any] | None = None,
        driver: dict[str, Any] | None = None,
        deviation: dict[str, Any] | None = None,
        eta: dict[str, Any] | None = None,
        route: dict[str, Any] | None = None,
        missing_parts: tuple[str, ...] = (),
    ) -> MissionSnapshot | None:
        """Assemble le snapshot pivot d'une mission.

        `observed_at` est l'**heure TMS** du cycle : c'est elle qui sert de
        référence à toutes les règles temporelles (F3).

        Rend `None` seulement si la mission elle-même est inexploitable — sans
        identifiant ou sans statut reconnu, il n'y a rien à surveiller.
        """
        mission_id = self.field(Entity.MISSION, "id", mission)
        status = self.mission_status(self.field(Entity.MISSION, "status", mission))
        if not mission_id or status is None:
            logger.warning(
                "Mission écartée (identifiant ou statut inexploitable) par le dialecte %s",
                self.tms_id,
            )
            return None

        reference = self.field(Entity.MISSION, "reference", mission)
        loading, unloading = self.stops(mission)
        origin, destination = self.mission_places(mission)
        mapped_vehicle = self.vehicle(vehicle)

        raw_events = events if events is not None else (self.embedded_events(mission) or [])
        mapped_events = self.events(
            [event for event in raw_events if isinstance(event, dict)],
            mission_id=str(mission_id),
            origin=origin,
            destination=destination,
        )

        return MissionSnapshot(
            # La provenance vient du dialecte : impossible de produire un
            # snapshot attribué à un autre TMS que celui qui l'a rendu.
            tms_id=self.tms_id,
            mission_id=str(mission_id),
            reference=str(reference) if reference else str(mission_id),
            status=status,
            driver=self.driver(driver),
            vehicle=mapped_vehicle,
            loading=loading,
            unloading=unloading,
            planned_route=self.route(route),
            # La position courante vient du véhicule : une seule source, pour
            # que deux champs ne puissent jamais se contredire dans une alerte.
            current_position=mapped_vehicle.current_position if mapped_vehicle else None,
            planned_times=PlannedTimes(
                pickup_at=parse_datetime(self.field(Entity.MISSION, "planned_pickup_at", mission)),
                delivery_at=parse_datetime(self.field(Entity.MISSION, "planned_delivery_at", mission)),
            ),
            actual_times=ActualTimes(
                pickup_at=parse_datetime(self.field(Entity.MISSION, "actual_pickup_at", mission)),
                delivery_at=parse_datetime(self.field(Entity.MISSION, "actual_delivery_at", mission)),
            ),
            deviation=self.deviation(deviation),
            eta=self.eta(eta),
            events=mapped_events,
            observed_at=observed_at,
            snapshot_complete=not missing_parts,
            missing_parts=missing_parts,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
