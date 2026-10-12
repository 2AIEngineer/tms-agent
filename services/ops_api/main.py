"""API d'exploitation : la seule porte publique du système.

    frontend / opérateur
        -> jeton Entra ID vérifié, rôles du TMS      (auth/)
        -> lectures en base (alertes, enquêtes, audit)
        -> lectures du TMS via le serveur MCP
        -> assistant (questions, rapports, actions)  F24-F26
        -> flux temps réel des alertes               (SSE)

Trois principes gouvernent ce service :

  - **il ne décide de rien.** Il lit, et il transmet les demandes d'action aux
    services qui en sont propriétaires, avec leurs garde-fous ;
  - **il n'a aucune connaissance du TMS.** Ni URL, ni endpoint, ni modèle
    JSON : le serveur MCP reste l'unique frontière ;
  - **une dépendance manquante dégrade, elle n'arrête pas.** Sans serveur MCP,
    les alertes restent consultables ; sans passerelle d'inférence, l'assistant
    répond qu'il est indisponible et le reste fonctionne.

Lancement :
    uv run python -m services.ops_api.main
"""

import contextlib
import logging
from collections.abc import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.agents.assistant.actions import AssistantActions
from services.agents.assistant.service import AssistantService
from services.agents.assistant.tools import ASSISTANT_READ_TOOLS
from services.agents.config import get_settings as get_agent_settings
from services.agents.integrations.litellm_client import LiteLlmClient
from services.agents.integrations.mcp_client import AgentMcpClient
from services.agents.operations.driver_call import DriverCallService
from shared.channels import ConsoleChannel
from shared.mapped_dto import NotificationChannel
from shared.utils.db import Database

from .auth import TokenVerifier
from .config import OpsApiSettings, get_settings
from .dependencies import AppState
from .mcp import OPS_TOOLS, OpsMcp
from .repository import OpsRepository
from .routers import alerts, assistant, health, missions, sse

logger = logging.getLogger(__name__)


def build_app(settings: OpsApiSettings | None = None) -> FastAPI:
    """Construit l'application, dépendances comprises.

    Une fonction et non un objet de module : les tests en obtiennent une
    instance isolée, avec leur propre base et leurs propres doubles.
    """
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        database = Database(settings.database_url)
        app.state.ops = AppState(
            settings=settings,
            database=database,
            repository=OpsRepository(),
            verifier=_verifier(settings),
        )
        # Le client MCP et l'assistant sont montés dans une pile de contextes :
        # ce qui a pu être ouvert est fermé, même si une étape échoue.
        async with contextlib.AsyncExitStack() as stack:
            await _mount_tms(stack, app, settings, database)
            logger.info(
                "API d'exploitation sur http://%s:%d — authentification %s",
                settings.host,
                settings.port,
                "DÉSACTIVÉE (développement)" if settings.auth_disabled else "Entra ID",
            )
            yield
        await database.dispose()

    app = FastAPI(
        title="Orchestrateur de suivi des missions — API d'exploitation",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    for router in (health.router, alerts.router, missions.router, assistant.router, sse.router):
        app.include_router(router)
    return app


def _verifier(settings: OpsApiSettings) -> TokenVerifier | None:
    """Vérificateur de jetons, si Entra est configuré.

    Son absence n'empêche pas le démarrage : la configuration manquante est
    signalée, et les routes protégées rendent 503 — ce qui est plus clair, pour
    qui déploie, qu'un conteneur qui refuse de démarrer sans dire pourquoi.
    """
    if settings.auth_disabled:
        logger.warning(
            "OPS_API_AUTH_DISABLED=true : toutes les requêtes seront acceptées "
            "sous l'identité '%s'. Mode développement uniquement.",
            settings.dev_operator_id,
        )
        return None
    if not settings.entra_tenant_id or not settings.entra_audience:
        logger.error(
            "ENTRA_TENANT_ID et ENTRA_AUDIENCE absents : les routes protégées "
            "répondront 503 jusqu'à leur configuration."
        )
        return None
    return TokenVerifier(
        jwks_url=settings.jwks_url,
        issuer=settings.issuer,
        audience=settings.entra_audience,
        cache_seconds=settings.jwks_cache_seconds,
    )


async def _mount_tms(
    stack: contextlib.AsyncExitStack,
    app: FastAPI,
    settings: OpsApiSettings,
    database: Database,
) -> None:
    """Branche le serveur MCP et l'assistant, sans empêcher le démarrage.

    Un serveur MCP éteint ne doit pas priver l'exploitation de ses alertes :
    elles sont en base, et c'est le cas d'usage le plus fréquent. Les routes
    qui dépendent du TMS rendent alors 503, ce que l'interface sait afficher.
    """
    try:
        ops_client = await stack.enter_async_context(
            AgentMcpClient(settings.mcp_server_url, allowed=OPS_TOOLS)
        )
        assistant_client = await stack.enter_async_context(
            AgentMcpClient(settings.mcp_server_url, allowed=ASSISTANT_READ_TOOLS)
        )
    except Exception as exception:
        logger.error(
            "Serveur MCP injoignable (%s) : lectures du TMS et assistant indisponibles — %s",
            settings.mcp_server_url,
            exception,
        )
        return

    app.state.ops.mcp = OpsMcp(ops_client)

    agent_settings = get_agent_settings()
    llm = LiteLlmClient(
        agent_settings.litellm_base_url,
        agent_settings.litellm_api_key,
        model=agent_settings.model,
        temperature=agent_settings.temperature,
        timeout_s=agent_settings.request_timeout_s,
        max_output_tokens=agent_settings.max_output_tokens,
    )
    stack.push_async_callback(llm.aclose)
    calls = (
        DriverCallService(ConsoleChannel(NotificationChannel.CALL), guardrails=agent_settings.guardrails())
        if agent_settings.driver_call_enabled
        else None
    )
    app.state.ops.assistant = AssistantService(
        llm,
        assistant_client,
        AssistantActions(database, assistant_client, driver_calls=calls),
        database=database,
        action_ttl=agent_settings.assistant_action_ttl,
        max_input_tokens=agent_settings.max_input_tokens,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    settings = get_settings()
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
