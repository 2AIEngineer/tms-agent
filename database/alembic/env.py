"""Environnement Alembic.

Deux écarts volontaires avec le gabarit par défaut :

  - l'URL vient du `.env` du projet, jamais de `alembic.ini` : il n'y a qu'une
    source de vérité pour la connexion, et aucun mot de passe dans un fichier
    versionné ;
  - `include_schemas` et `version_table_schema` cantonnent Alembic au schéma
    `orchestrator`. Sans cela, l'autogénération verrait aussi les tables de
    Langfuse et de LiteLLM présentes sur le même serveur, et proposerait de les
    supprimer.
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from database.schema import SCHEMA, metadata  # noqa: E402
from services.monitoring.config import get_settings  # noqa: E402
from shared.utils.db import to_async_dsn  # noqa: E402

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers=False` est indispensable : la valeur par défaut
    # de `fileConfig` est True, ce qui **éteint tous les loggers déjà créés**.
    # Appliquer les migrations depuis un processus applicatif rendrait alors
    # muets les journaux de ce processus — y compris ceux de la surveillance et
    # des notifications, qui sont la seule fenêtre sur ce que le système fait.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Une URL déjà présente l'emporte : c'est ainsi que les tests font tourner les
# migrations réelles contre leur base dédiée, sans toucher au `.env`.
if not (config.get_main_option("sqlalchemy.url", None) or "").strip():
    config.set_main_option("sqlalchemy.url", to_async_dsn(get_settings().database_url))

target_metadata = metadata


def _common_options() -> dict[str, object]:
    return {
        "target_metadata": target_metadata,
        "include_schemas": True,
        "version_table_schema": SCHEMA,
        # Les changements de type sont détectés : un VARCHAR(64) devenu
        # VARCHAR(128) doit produire une migration, pas passer inaperçu.
        "compare_type": True,
        "compare_server_default": True,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_common_options(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_common_options())
    with context.begin_transaction():
        # Le schéma doit exister avant la table de version d'Alembic.
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
