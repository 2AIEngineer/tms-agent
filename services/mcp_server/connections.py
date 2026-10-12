"""Registre des TMS raccordés.

Un TMS raccordé est un quadruplet indissociable : un **dialecte**, un
**transport** qui l'applique, un **mapper** qui traduit avec lui, et des
**garde-fous** qui lui sont propres. Les réunir dans un même objet rend
impossible la faute la plus coûteuse de cette architecture — un connecteur qui
lit un TMS et un mapper qui en traduit un autre.

Les garde-fous sont **par connexion** et non globaux : le quota appartient au
TMS, et un client dont l'API est généreuse n'a pas à subir la limite d'un autre.

La configuration des connexions vit dans un fichier YAML lorsqu'il y en a
plusieurs, et se réduit aux variables d'environnement quand il n'y en a qu'une
— le cas du POC.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .connectors.dialect import TmsDialect, load_named_dialect
from .connectors.http_tms import HttpTmsConnector
from .connectors.profile import build_profile
from .guardrails import TmsGuardrails
from .mappers import PivotMapper
from .recipients import RecipientResolver
from .snapshots import SnapshotService

logger = logging.getLogger(__name__)


class ConnectionSpec(BaseModel):
    """Déclaration d'un TMS à raccorder."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tms_id: str = Field(pattern=r"^[a-z0-9_]+$")
    dialect: str = Field(description="Nom du dialecte, dans connectors/dialects/.")
    base_url: str = Field(description="Racine d'API, si le dialecte n'en fixe pas.")
    api_key_env: str | None = Field(
        default=None,
        description="Variable d'environnement portant le secret. Jamais le secret.",
    )
    label: str | None = None
    enabled: bool = True

    # Quotas propres à ce TMS : le plafond appartient au TMS, pas à nous.
    max_requests_per_second: float = Field(default=25.0, gt=0)
    max_concurrent_requests: int = Field(default=8, ge=1)
    timeout_s: float = Field(default=5.0, gt=0)
    mission_concurrency: int = Field(default=6, ge=1)

    @model_validator(mode="after")
    def _identifier_matches_dialect(self) -> Self:
        """L'identifiant doit correspondre au dialecte, sauf mention explicite.

        Deux connexions sur le même dialecte sont légitimes — deux instances du
        même logiciel chez deux clients — mais elles doivent alors porter des
        identifiants distincts, puisque c'est lui qui cantonne la déduplication.
        """
        return self


@dataclass(slots=True)
class TmsConnection:
    """Un TMS raccordé, avec tout ce qu'il faut pour le lire et le traduire."""

    tms_id: str
    label: str
    dialect: TmsDialect
    connector: HttpTmsConnector
    mapper: PivotMapper
    snapshots: SnapshotService
    recipients: RecipientResolver
    guardrails: TmsGuardrails

    @classmethod
    def open(cls, spec: ConnectionSpec) -> "TmsConnection":
        dialect = load_named_dialect(spec.dialect)
        guardrails = TmsGuardrails(
            max_requests_per_second=spec.max_requests_per_second,
            max_concurrent=spec.max_concurrent_requests,
        )
        api_key = os.environ.get(spec.api_key_env) if spec.api_key_env else None
        connector = HttpTmsConnector(
            spec.base_url,
            dialect,
            api_key=api_key,
            timeout_s=spec.timeout_s,
            guardrails=guardrails,
            profile=build_profile(dialect),
        )
        mapper = PivotMapper(dialect, tms_id=spec.tms_id)
        return cls(
            tms_id=spec.tms_id,
            label=spec.label or dialect.label,
            dialect=dialect,
            connector=connector,
            mapper=mapper,
            snapshots=SnapshotService(
                connector,  # type: ignore[arg-type]
                mapper,
                mission_concurrency=spec.mission_concurrency,
            ),
            recipients=RecipientResolver(connector),  # type: ignore[arg-type]
            guardrails=guardrails,
        )

    @classmethod
    def from_parts(
        cls,
        connector: HttpTmsConnector,
        *,
        tms_id: str | None = None,
        mission_concurrency: int = 6,
        guardrails: TmsGuardrails | None = None,
    ) -> "TmsConnection":
        """Construit une connexion autour d'un connecteur déjà ouvert.

        Sert à injecter un transport simulé, ou un connecteur dont le client
        HTTP est fourni par l'appelant.
        """
        dialect = connector.dialect
        resolved_id = tms_id or dialect.tms_id
        mapper = PivotMapper(dialect, tms_id=resolved_id)
        return cls(
            tms_id=resolved_id,
            label=dialect.label,
            dialect=dialect,
            connector=connector,
            mapper=mapper,
            snapshots=SnapshotService(
                connector,  # type: ignore[arg-type]
                mapper,
                mission_concurrency=mission_concurrency,
            ),
            recipients=RecipientResolver(connector),  # type: ignore[arg-type]
            guardrails=guardrails or TmsGuardrails(max_requests_per_second=25.0, max_concurrent=8),
        )

    async def aclose(self) -> None:
        await self.connector.aclose()


class UnknownTms(KeyError):
    """TMS demandé qui n'est pas raccordé."""


@dataclass(slots=True)
class TmsRegistry:
    """Les TMS raccordés, interrogeables par identifiant."""

    connections: dict[str, TmsConnection]

    @property
    def default_tms_id(self) -> str:
        """TMS visé quand l'appelant n'en nomme aucun.

        N'a de sens qu'avec une seule connexion. Avec plusieurs, un appel sans
        `tms_id` est une erreur : deviner lequel reviendrait à surveiller un
        client et pas l'autre, au hasard de l'ordre de configuration.
        """
        return next(iter(self.connections))

    @property
    def single(self) -> bool:
        return len(self.connections) == 1

    def get(self, tms_id: str | None = None) -> TmsConnection:
        if tms_id is None:
            if not self.single:
                raise UnknownTms(
                    f"plusieurs TMS raccordés ({', '.join(sorted(self.connections))}) : préciser 'tms_id'"
                )
            return self.connections[self.default_tms_id]
        try:
            return self.connections[tms_id]
        except KeyError:
            raise UnknownTms(
                f"TMS inconnu : '{tms_id}' (raccordés : {', '.join(sorted(self.connections))})"
            ) from None

    def all(self) -> list[TmsConnection]:
        return list(self.connections.values())

    async def aclose(self) -> None:
        for connection in self.connections.values():
            await connection.aclose()


def load_specs(path: Path) -> list[ConnectionSpec]:
    """Lit un fichier de connexions.

    Format attendu :

        connections:
          - { tms_id: acme, dialect: fleetlink, base_url: https://…, api_key_env: ACME_KEY }
          - { tms_id: x_tms, dialect: x_tms, base_url: http://localhost:8000/v1 }
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("connections"), list):
        raise ValueError(f"{path.name} : attendu un mapping avec une liste 'connections'")

    specs = [ConnectionSpec.model_validate(entry) for entry in raw["connections"]]
    enabled = [spec for spec in specs if spec.enabled]
    if not enabled:
        raise ValueError(f"{path.name} : aucune connexion active")

    identifiers = [spec.tms_id for spec in enabled]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        # Deux connexions de même identifiant rendraient la déduplication et
        # l'audit ambigus : exactement le défaut que cette dimension corrige.
        raise ValueError(f"{path.name} : identifiants en doublon {duplicates}")
    return enabled


def build_registry(specs: list[ConnectionSpec]) -> TmsRegistry:
    registry = TmsRegistry({spec.tms_id: TmsConnection.open(spec) for spec in specs})
    logger.info(
        "TMS raccordés : %s",
        ", ".join(f"{c.tms_id} ({c.dialect.tms_id})" for c in registry.all()),
    )
    return registry


def spec_from_env(settings: Any) -> ConnectionSpec:
    """Connexion unique décrite par les variables d'environnement.

    Le cas du POC : un seul TMS, aucun fichier de connexions à maintenir.
    """
    return ConnectionSpec(
        tms_id=settings.tms_id,
        dialect=settings.tms_dialect,
        base_url=settings.tms_api_root,
        api_key_env="TMS_API_KEY" if settings.tms_api_key else None,
        max_requests_per_second=settings.tms_max_requests_per_second,
        max_concurrent_requests=settings.tms_max_concurrent_requests,
        timeout_s=settings.tms_timeout_s,
    )
