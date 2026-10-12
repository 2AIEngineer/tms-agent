"""Serveur MCP : unique frontière d'intégration avec le TMS.

Ni le monitoring ni l'agent n'appellent l'API du TMS directement. Tout passe
par ces outils, ce qui concentre en un seul endroit les garde-fous de débit,
les délais, les reprises et la traduction vers le contrat pivot.

Transport HTTP streamable, **sans état** : plusieurs réplicas peuvent répondre
derrière un même point d'entrée, aucune session n'étant attachée à l'un d'eux.

Lancement :
    uv run python -m services.mcp_server.server
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from .config import McpSettings, get_settings
from .context import McpContext
from .tools import missions, monitoring, participants, registry, tracking

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
Frontière de lecture vers le TMS (Transport Management System).

Trois principes à connaître avant d'utiliser ces outils :

1. **Lecture seule.** Aucun outil ne modifie le TMS. Les alertes, les
   notifications et les décisions de l'agent vivent dans sa propre base.
2. **Heure du TMS.** Tout raisonnement temporel se fonde sur `get_tms_time`,
   jamais sur l'heure réelle : l'horloge du TMS peut être accélérée.
3. **TMS explicite.** Tous les outils exigent un `tms_id`, même lorsqu'un seul
   TMS est raccordé. `list_tms` donne la liste des TMS valides et leurs
   capacités : un TMS `degraded` a des règles qui ne déclencheront jamais, et
   l'absence d'alerte n'y signifie pas l'absence d'anomalie.
4. **Erreurs explicites.** Chaque réponse porte `ok`. Si `ok` vaut `false`,
   lire `error.code` et `error.retryable` : `MISSION_NOT_FOUND` est un fait
   métier, `TMS_TIMEOUT` ou `TMS_UNAVAILABLE` sont des incidents techniques
   qu'il faut réessayer sans en tirer de conclusion sur la mission.

Un champ absent ou `null` signifie « le TMS ne l'a pas renseigné », et non
« la valeur est nulle ». Ne jamais conclure d'une absence de donnée.
"""


def build_server(
    settings: McpSettings | None = None,
    context: McpContext | None = None,
) -> tuple[FastMCP, McpContext]:
    """Construit le serveur et son contexte, sans les démarrer.

    Les deux sont rendus séparément pour que les tests puissent interroger les
    outils en mémoire, et fermer proprement le client HTTP.
    """
    resolved = settings or get_settings()
    resolved_context = context or McpContext.build(resolved)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncGenerator[None]:
        logger.info("Serveur MCP prêt — TMS : %s", resolved.tms_api_root)
        try:
            yield
        finally:
            await resolved_context.aclose()
            logger.info("Connexions TMS fermées")

    mcp = FastMCP(
        name="tms-agent-mcp",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
    )

    for module in (registry, missions, tracking, participants, monitoring):
        module.register(mcp, resolved_context)

    return mcp, resolved_context


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    server, _ = build_server(settings)
    server.run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        stateless_http=settings.mcp_stateless,
    )


if __name__ == "__main__":
    main()
