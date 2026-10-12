"""Accès PostgreSQL : moteur, transactions et verrous consultatifs.

SQLAlchemy Core en asynchrone (asyncpg). Pas d'ORM : les repositories écrivent
des requêtes explicites, parce que la correction du système repose sur des
mécanismes SQL précis (`ON CONFLICT` sur index partiel, `pg_advisory_xact_lock`,
`FOR UPDATE SKIP LOCKED`) qu'une couche d'objets rendrait opaques.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

ASYNC_DRIVER = "postgresql+asyncpg"


def to_async_dsn(url: str) -> str:
    """Normalise une URL PostgreSQL vers le pilote asynchrone.

    Le `.env` porte une URL `postgresql://` standard, lisible par `psql` et par
    tous les outils ; la convertir ici évite d'avoir deux URL à maintenir, qui
    finiraient par diverger.
    """
    for prefix in ("postgresql+asyncpg://", f"{ASYNC_DRIVER}://"):
        if url.startswith(prefix):
            return url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return f"{ASYNC_DRIVER}://{url[len(prefix) :]}"
    raise ValueError(f"URL PostgreSQL non reconnue : {url!r}")


class Database:
    """Moteur de connexions partagé par les repositories d'un service.

    Un seul moteur par processus : le pool est la ressource à partager, et en
    créer plusieurs épuiserait `max_connections` côté serveur.
    """

    def __init__(self, url: str, *, pool_size: int = 5, max_overflow: int = 5, echo: bool = False) -> None:
        self._engine: AsyncEngine = create_async_engine(
            to_async_dsn(url),
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,  # une connexion coupée par le réseau est remplacée, pas rendue
            echo=echo,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AsyncConnection]:
        """Ouvre une connexion et une transaction, validée à la sortie du bloc.

        Tout ce qui doit être atomique passe par un seul appel : création
        d'alerte, mise en file des notifications et marquage de l'événement
        traité sont écrits ensemble ou pas du tout.
        """
        async with self._engine.begin() as connection:
            yield connection

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection]:
        """Connexion en lecture, sans transaction explicite."""
        async with self._engine.connect() as connection:
            yield connection

    async def ping(self) -> bool:
        """Vérifie que la base répond, pour les sondes de démarrage."""
        try:
            async with self.connection() as connection:
                await connection.execute(text("select 1"))
            return True
        except Exception:
            logger.exception("PostgreSQL injoignable")
            return False

    async def dispose(self) -> None:
        await self._engine.dispose()


async def advisory_xact_lock(connection: AsyncConnection, key: str) -> None:
    """Verrou consultatif tenu jusqu'à la fin de la transaction courante.

    Sérialise les écritures portant sur une même clé logique — ici la clé de
    déduplication d'une alerte. Sans lui, le contrôle du cooldown resterait un
    « lire puis écrire » : deux processus pourraient tous deux constater que le
    cooldown est écoulé et créer chacun une alerte, l'index unique partiel ne
    les en empêchant pas si l'alerte précédente est close.

    `hashtext` plutôt que la chaîne : les verrous consultatifs PostgreSQL
    s'adressent par entier. La collision de hachage est possible et sans
    conséquence : elle sérialise deux clés qui auraient pu l'être en parallèle.
    """
    await connection.execute(
        text("select pg_advisory_xact_lock(hashtext(:key))"),
        {"key": key},
    )


async def try_advisory_lock(connection: AsyncConnection, key: int) -> bool:
    """Tente de prendre un verrou consultatif de session, sans attendre.

    Porte la garantie d'instance unique du `monitoring_worker` : le verrou est
    tenu par la connexion, donc libéré automatiquement si le processus meurt —
    aucun verrou orphelin à nettoyer à la main.
    """
    result = await connection.execute(
        text("select pg_try_advisory_lock(:key)"),
        {"key": key},
    )
    return bool(result.scalar())


async def advisory_unlock(connection: AsyncConnection, key: int) -> bool:
    result = await connection.execute(text("select pg_advisory_unlock(:key)"), {"key": key})
    return bool(result.scalar())
