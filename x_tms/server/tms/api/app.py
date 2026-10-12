from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tms import __version__
from tms.api.routers import meta, missions, users, vehicles
from tms.config import get_settings
from tms.db import init_db
from tms.errors import install_error_handlers
from tms.seed import seed_reference_data

DESCRIPTION = """
Faux TMS (Transport Management System) exposant le cycle de vie des missions de transport.

* **Lecture seule** : l'API est consommée (via un serveur MCP) sans jamais être modifiée.
  Les données sont produites par les systèmes internes du TMS (télématique, appli chauffeur),
  simulés ici par `tms start-missions`.
* **Faits, pas de décisions** : positions, événements, écart à l'itinéraire, ETA.
* **Pull uniquement** : aucun push ; suivre les changements par polling (`updated_since`, `since`).
* Horodatages ISO 8601 UTC, listes paginées par curseur, erreurs RFC 7807 (`application/problem+json`).
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    seed_reference_data()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="TMS API",
        version=__version__,
        description=DESCRIPTION,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url="/v1/redoc",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    for module in (missions, vehicles, users, meta):
        app.include_router(module.router, prefix="/v1")
    return app


app = create_app()
