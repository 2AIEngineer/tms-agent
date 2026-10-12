"""Dialecte d'un TMS : la correspondance entre nos définitions et les siennes.

Le connecteur définit **ce qu'il sait lire** — `list_missions`, `get_route`,
`deviation.current_offset_km`. Un dialecte répond à une seule question, pour un
TMS donné : *« quelle est la correspondance de cela chez lui ? »*

Ce qui est déclaratif ici est ce qui relève de la **donnée** : chemins d'URL,
noms de champs, valeurs d'énumération, style de pagination, mode
d'authentification. Ce qui relève de la **logique** — calculer un écart que le
TMS ne fournit pas, réduire une tournée multi-arrêts à une origine et une
destination — reste du code, dans un `TmsProfile`. Rendre la config capable de
tout en ferait un langage de programmation sans typage ni tests, dont les
erreurs n'apparaîtraient qu'en production.

Le dialecte est **validé au chargement** contre le contrat du connecteur : une
opération inconnue, un champ obligatoire non mappé ou une énumération incomplète
refusent la connexion. Le même principe que les règles métier, refusées au dépôt
du fichier plutôt que découvertes au milieu d'un cycle.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.mapped_dto import (
    DeviationSeverity,
    EventSource,
    EventType,
    MissionStatus,
    VehicleStatus,
)

DIALECTS_DIRECTORY = Path(__file__).parent / "dialects"


class Operation(StrEnum):
    """Les lectures que le connecteur sait faire.

    Cette liste est le contrat : un dialecte doit donner une correspondance à
    chacune, ou la déclarer absente. Elle ne grandit que si le connecteur
    apprend une nouvelle capacité, jamais pour s'adapter à un TMS particulier.
    """

    HEALTH = "health"
    LIST_MISSIONS = "list_missions"
    GET_MISSION = "get_mission"
    GET_MISSION_EVENTS = "get_mission_events"
    GET_ROUTE = "get_route"
    GET_DEVIATION = "get_deviation"
    GET_ETA = "get_eta"
    GET_VEHICLE = "get_vehicle"
    GET_VEHICLE_POSITIONS = "get_vehicle_positions"
    GET_DRIVER = "get_driver"
    GET_USER = "get_user"
    LIST_USERS_BY_ROLE = "list_users_by_role"


# Sans ces deux-là, il n'y a rien à surveiller : aucun dialecte ne peut les
# déclarer absentes. Toutes les autres opérations sont facultatives, et leur
# absence dégrade la surveillance de façon documentée (cf. rapport de capacité).
REQUIRED_OPERATIONS: frozenset[Operation] = frozenset(
    {Operation.HEALTH, Operation.LIST_MISSIONS, Operation.GET_MISSION}
)


class Entity(StrEnum):
    """Les objets que les mappers traduisent."""

    MISSION = "mission"
    EVENT = "event"
    VEHICLE = "vehicle"
    DRIVER = "driver"
    USER = "user"
    DEVIATION = "deviation"
    ETA = "eta"
    ROUTE = "route"
    WAYPOINT = "waypoint"
    POSITION = "position"
    HEALTH = "health"


# Champs sans lesquels l'objet pivot n'a pas de sens. Un dialecte qui ne les
# mappe pas est refusé : mieux vaut un refus de connexion explicite qu'une
# mission sans identifiant, invisible pour la déduplication et pour l'audit.
REQUIRED_FIELDS: dict[Entity, frozenset[str]] = {
    Entity.MISSION: frozenset({"id", "status"}),
    Entity.EVENT: frozenset({"id", "type", "occurred_at"}),
    Entity.VEHICLE: frozenset({"id"}),
    Entity.DRIVER: frozenset({"id"}),
    Entity.USER: frozenset({"id"}),
    Entity.POSITION: frozenset({"lat", "lon"}),
    Entity.HEALTH: frozenset({"time"}),
}


class PaginationStyle(StrEnum):
    NONE = "none"
    CURSOR = "cursor"
    OFFSET = "offset"
    PAGE = "page"


class AuthStyle(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    BASIC = "basic"


class DialectModel(BaseModel):
    """Base stricte : une clé mal orthographiée doit refuser le dialecte.

    L'ignorer laisserait croire qu'une correspondance est déclarée alors que le
    connecteur lirait toujours le nom par défaut.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class OperationSpec(DialectModel):
    """Correspondance d'une opération du connecteur chez ce TMS."""

    path: str | None = Field(
        default=None,
        description="Chemin relatif à la racine d'API, avec ses paramètres : "
        "/resources/missions/{mission_id}/way",
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description='Paramètres de requête fixes ou gabarits : {"state": "{statuses}"}.',
    )
    result_path: str | None = Field(
        default=None,
        description="Chemin de l'objet utile quand la réponse est enveloppée, "
        "ex. 'data' pour {\"data\": {...}}. Sans effet sur les listes paginées, "
        "dont le chemin est donné par 'pagination.items_path'.",
    )
    unsupported: bool = Field(
        default=False,
        description="Le TMS ne sait pas rendre cette information. Déclaré plutôt que subi : "
        "le rapport de capacité en déduit les règles qui ne déclencheront jamais.",
    )

    @model_validator(mode="after")
    def _path_or_unsupported(self) -> Self:
        if self.unsupported and self.path:
            raise ValueError("une opération déclarée 'unsupported' ne doit pas porter de 'path'")
        if not self.unsupported and not self.path:
            raise ValueError("renseigner 'path', ou déclarer 'unsupported: true'")
        return self


class ListSpec(DialectModel):
    """Où trouver une liste, et quelle entité décrit ses éléments."""

    path: str = Field(description="Chemin de la liste dans le payload, ex. 'geometry.points'.")
    item: Entity = Field(description="Entité décrivant un élément de la liste.")


class PaginationSpec(DialectModel):
    """Dialecte de pagination.

    Quatre styles couvrent l'essentiel des API REST. Un TMS qui en emploie un
    autre (en-tête `Link`, jeton signé) passe par un `TmsProfile`.
    """

    style: PaginationStyle = PaginationStyle.NONE
    items_path: str = Field(default="data", description="Chemin des éléments dans une page.")
    cursor_param: str = "cursor"
    cursor_path: str = Field(default="pagination.cursor", description="Curseur de la page suivante.")
    has_more_path: str | None = Field(
        default="pagination.has_more",
        description="Drapeau de page suivante. Absent, on s'arrête quand le curseur est vide.",
    )
    limit_param: str | None = "limit"
    page_size: int = Field(default=200, ge=1)
    offset_param: str = "offset"
    page_param: str = "page"
    first_page: int = Field(default=1, description="Numéro de la première page (0 ou 1 selon le TMS).")
    max_pages: int = Field(
        default=50,
        ge=1,
        description="Plafond de sécurité : une pagination qui ne se termine jamais "
        "ne doit pas bloquer un cycle de surveillance.",
    )


class AuthSpec(DialectModel):
    style: AuthStyle = AuthStyle.NONE
    header: str = Field(default="Authorization", description="En-tête porteur du secret.")
    prefix: str = Field(default="Bearer", description="Préfixe de la valeur, vide si aucun.")
    secret_env: str | None = Field(
        default=None,
        description="Variable d'environnement contenant le secret. Jamais le secret lui-même : "
        "un dialecte est versionné.",
    )

    @model_validator(mode="after")
    def _secret_required(self) -> Self:
        if self.style is not AuthStyle.NONE and not self.secret_env:
            raise ValueError(f"le mode '{self.style.value}' exige 'secret_env'")
        return self


class TmsDialect(DialectModel):
    """Correspondance complète entre le connecteur et un TMS."""

    tms_id: str = Field(pattern=r"^[a-z0-9_]+$")
    label: str
    description: str | None = None
    api_root: str | None = Field(
        default=None,
        description="Racine d'API, si elle est fixe. Sinon fournie par la configuration "
        "du serveur (TMS_BASE_URL).",
    )
    profile: str | None = Field(
        default=None,
        description="Nom du TmsProfile à appliquer, pour ce que la config ne peut pas exprimer.",
    )

    auth: AuthSpec = Field(default_factory=AuthSpec)
    pagination: PaginationSpec = Field(default_factory=PaginationSpec)
    operations: dict[Operation, OperationSpec]
    # Correspondances scalaires : chemin chez nous -> chemin chez eux, les deux pointés.
    fields: dict[Entity, dict[str, str]] = Field(default_factory=dict)
    lists: dict[str, ListSpec] = Field(default_factory=dict)
    # Valeur canonique -> valeurs du TMS. Une liste, car un TMS a souvent des
    # statuts plus fins que les nôtres et plusieurs doivent se replier sur un seul.
    enums: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_contract(self) -> Self:
        """Vérifie le dialecte contre le contrat du connecteur.

        C'est ici que se joue la promesse : brancher un TMS échoue au
        chargement du dialecte, avec un message qui dit quoi corriger, plutôt
        qu'à 3 h du matin au milieu d'un cycle.
        """
        problems: list[str] = []

        missing_operations = set(Operation) - set(self.operations)
        if missing_operations:
            problems.append(
                "opérations sans correspondance : "
                + ", ".join(sorted(operation.value for operation in missing_operations))
                + " (renseigner 'path' ou 'unsupported: true')"
            )

        for operation in sorted(REQUIRED_OPERATIONS):
            spec = self.operations.get(operation)
            if spec is not None and spec.unsupported:
                problems.append(
                    f"'{operation.value}' ne peut pas être déclarée absente : "
                    "sans elle il n'y a aucune mission à surveiller"
                )

        for entity, required in REQUIRED_FIELDS.items():
            if not self._entity_reachable(entity):
                continue
            mapped = set(self.fields.get(entity, {}))
            for field_name in sorted(required - mapped):
                problems.append(f"champ obligatoire non mappé : {entity.value}.{field_name}")

        problems.extend(self._validate_enums())

        # Pydantic valide déjà `item` contre `Entity` ; ce qui mérite un
        # contrôle, c'est qu'une liste déclarée pointe une entité réellement
        # mappée, sinon ses éléments seraient tous vides en silence.
        for name, list_spec in self.lists.items():
            if list_spec.item not in self.fields:
                problems.append(f"liste '{name}' : l'entité '{list_spec.item.value}' n'a aucun champ mappé")

        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _entity_reachable(self, entity: Entity) -> bool:
        """Une entité inatteignable n'a pas à être mappée.

        Inutile d'exiger les champs du véhicule d'un TMS qui ne sait pas en
        rendre : le rapport de capacité signalera la conséquence, et exiger le
        mapping d'une ressource absente n'aurait aucun sens.
        """
        gate = {
            Entity.VEHICLE: Operation.GET_VEHICLE,
            Entity.DRIVER: Operation.GET_DRIVER,
            Entity.USER: Operation.GET_USER,
            Entity.DEVIATION: Operation.GET_DEVIATION,
            Entity.ETA: Operation.GET_ETA,
            Entity.ROUTE: Operation.GET_ROUTE,
        }.get(entity)
        if gate is None:
            return True
        spec = self.operations.get(gate)
        return spec is None or not spec.unsupported

    def _validate_enums(self) -> list[str]:
        """Vérifie que les énumérations déclarées parlent de valeurs canoniques existantes."""
        catalogue: dict[str, type[StrEnum]] = {
            "mission_status": MissionStatus,
            "vehicle_status": VehicleStatus,
            "event_type": EventType,
            "event_source": EventSource,
            "deviation_severity": DeviationSeverity,
        }
        problems: list[str] = []

        for name, mapping in self.enums.items():
            enum_type = catalogue.get(name)
            if enum_type is None:
                problems.append(f"énumération inconnue : '{name}' (attendu : {', '.join(sorted(catalogue))})")
                continue
            known = {member.value for member in enum_type}
            for canonical in sorted(set(mapping) - known):
                problems.append(f"{name} : valeur canonique inconnue '{canonical}'")

        # Les statuts de mission sont la seule énumération dont la couverture
        # est exigée : une mission dont le statut n'est pas reconnu est écartée
        # de la surveillance, et un trou ici se traduit par une flotte
        # partiellement suivie sans que personne ne le voie.
        statuses = self.enums.get("mission_status")
        if statuses is not None:
            watched = {MissionStatus.PLANNED.value, MissionStatus.IN_PROGRESS.value}
            for canonical in sorted(watched - set(statuses)):
                problems.append(
                    f"mission_status : '{canonical}' sans correspondance — "
                    "ces missions ne seraient jamais surveillées"
                )
        return problems

    # --- Interrogation -------------------------------------------------------

    def operation(self, operation: Operation) -> OperationSpec:
        return self.operations[operation]

    def supports(self, operation: Operation) -> bool:
        spec = self.operations.get(operation)
        return spec is not None and not spec.unsupported

    def their_path(self, entity: Entity, our_path: str) -> str | None:
        """Chemin de ce champ chez ce TMS, ou None s'il n'a pas de correspondance."""
        return self.fields.get(entity, {}).get(our_path)

    def list_spec(self, name: str) -> ListSpec | None:
        return self.lists.get(name)

    def canonical(self, enum_name: str, raw: Any) -> str | None:
        """Traduit une valeur du TMS vers notre vocabulaire.

        La comparaison ignore la casse et les espaces : les API emploient
        indifféremment `IN_PROGRESS`, `in_progress` ou `In Progress`, et en
        faire trois cas distincts à déclarer serait une source d'erreur inutile.
        """
        if not isinstance(raw, str):
            return None
        needle = raw.strip().lower()
        for canonical_value, their_values in self.enums.get(enum_name, {}).items():
            if any(needle == value.strip().lower() for value in their_values):
                return canonical_value
        return None


def load_dialect(path: Path) -> TmsDialect:
    """Charge et valide un dialecte."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} : le fichier doit contenir un mapping décrivant un dialecte")
    return TmsDialect.model_validate(raw)


def load_named_dialect(name: str, directory: Path | None = None) -> TmsDialect:
    """Charge un dialecte livré, par son nom de fichier."""
    folder = directory or DIALECTS_DIRECTORY
    for suffix in (".yaml", ".yml"):
        candidate = folder / f"{name}{suffix}"
        if candidate.is_file():
            return load_dialect(candidate)
    available = sorted(p.stem for p in folder.glob("*.y*ml"))
    raise FileNotFoundError(f"Dialecte '{name}' introuvable dans {folder}. Disponibles : {available}")


def available_dialects(directory: Path | None = None) -> list[str]:
    folder = directory or DIALECTS_DIRECTORY
    return sorted(path.stem for path in folder.glob("*.y*ml"))
