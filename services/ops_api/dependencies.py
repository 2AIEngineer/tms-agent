"""Dépendances partagées par les routes.

Un seul endroit construit les objets coûteux — moteur de base, client MCP,
vérificateur de jetons — et les routes les reçoivent. Trois raisons :

  - un pool de connexions par requête épuiserait PostgreSQL ;
  - le client MCP maintient une session, qu'il faut ouvrir et fermer avec
    l'application ;
  - les tests remplacent une dépendance sans toucher aux routes.

L'authentification est elle-même une dépendance : une route qui oublierait de
la déclarer serait ouverte, ce qui ne doit jamais pouvoir arriver par
distraction. D'où `require_read` et `require_act`, qui sont les deux seules
façons d'entrer.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from services.agents.assistant.service import AssistantService
from shared.utils.db import Database

from .auth import AuthError, Permission, Principal, TokenVerifier, development_principal
from .config import OpsApiSettings
from .mcp import OpsMcp
from .repository import OpsRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AppState:
    """Ce que l'application tient ouvert pendant toute sa vie."""

    settings: OpsApiSettings
    database: Database
    repository: OpsRepository
    assistant: AssistantService | None = None
    mcp: OpsMcp | None = None
    verifier: TokenVerifier | None = None


def state(request: Request) -> AppState:
    app_state = getattr(request.app.state, "ops", None)
    if not isinstance(app_state, AppState):  # pragma: no cover - mauvais câblage
        raise HTTPException(status_code=503, detail="Application non initialisée")
    return app_state


def database(app_state: Annotated[AppState, Depends(state)]) -> Database:
    return app_state.database


def repository(app_state: Annotated[AppState, Depends(state)]) -> OpsRepository:
    return app_state.repository


def assistant(app_state: Annotated[AppState, Depends(state)]) -> AssistantService:
    if app_state.assistant is None:
        # L'assistant dépend du serveur MCP et du proxy d'inférence : si l'un
        # manque, les lectures d'alertes doivent continuer de fonctionner.
        raise HTTPException(
            status_code=503,
            detail="Assistant indisponible : serveur MCP ou passerelle d'inférence injoignable.",
        )
    return app_state.assistant


async def principal(
    app_state: Annotated[AppState, Depends(state)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Identifie l'appelant, ou refuse la requête.

    Le mode développement est vérifié **avant** le jeton, et journalisé à
    chaque requête : un service lancé par erreur avec l'authentification
    coupée doit le crier dans ses journaux, pas l'oublier.
    """
    settings = app_state.settings
    if settings.auth_disabled:
        logger.warning(
            "Requête acceptée sans authentification (OPS_API_AUTH_DISABLED) — mode développement uniquement"
        )
        return development_principal(settings.dev_operator_id, settings.dev_roles)

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Jeton porteur attendu.")
    if app_state.verifier is None:  # pragma: no cover - configuration incomplète
        raise HTTPException(
            status_code=503,
            detail="Authentification non configurée : ENTRA_TENANT_ID et ENTRA_AUDIENCE requis.",
        )
    try:
        return app_state.verifier.verify(authorization.split(" ", 1)[1].strip())
    except AuthError as error:
        raise HTTPException(status_code=error.status, detail=error.message) from error


def _requires(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    async def dependency(
        caller: Annotated[Principal, Depends(principal)],
    ) -> Principal:
        try:
            caller.require(permission)
        except AuthError as error:
            raise HTTPException(status_code=error.status, detail=error.message) from error
        return caller

    return dependency


# Les deux portes d'entrée. Une route déclare l'une ou l'autre ; il n'y a pas
# de troisième voie, et pas de route sans porte.
require_read = _requires(Permission.READ)
require_act = _requires(Permission.ACT)

State = Annotated[AppState, Depends(state)]
ReadAccess = Annotated[Principal, Depends(require_read)]
ActAccess = Annotated[Principal, Depends(require_act)]
Repository = Annotated[OpsRepository, Depends(repository)]
Db = Annotated[Database, Depends(database)]
Assistant = Annotated[AssistantService, Depends(assistant)]
