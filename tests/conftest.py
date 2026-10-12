"""Fixtures partagées par la suite de tests."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from database.schema import SCHEMA, metadata
from services.monitoring.rules.loader import RuleSet, load_rules
from shared.utils.db import Database, to_async_dsn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES_DIRECTORY = PROJECT_ROOT / "services/monitoring/rules/definitions"

# Base dédiée aux tests. Distincte de la base de développement : la suite vide
# les tables entre chaque test, ce qui serait destructeur ailleurs.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:admin@localhost:5432/tms_agent_test_db",
)


@pytest.fixture
def shipped_rules() -> RuleSet:
    """Les règles réellement livrées dans `definitions/`.

    Les tests du moteur tournent sur elles plutôt que sur des règles inventées :
    c'est ce qui garantit que les règles du dépôt sont valides et déclenchent
    bien sur les situations qu'elles décrivent. Le chemin est absolu pour que la
    suite passe quel que soit le répertoire de lancement.
    """
    ruleset = load_rules(RULES_DIRECTORY)
    assert not ruleset.errors, f"règles livrées invalides : {[str(e) for e in ruleset.errors]}"
    return ruleset


# --- Base de données -----------------------------------------------------------


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Applique les migrations Alembic réelles sur la base de test.

    Les migrations plutôt qu'un `create_all` depuis la métadonnée : c'est le
    chemin qui sera emprunté en production. Une migration qui divergerait du
    schéma déclaré doit faire échouer les tests, pas passer inaperçue.
    """
    config = Config(str(PROJECT_ROOT / "database/alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "database/alembic"))
    config.set_main_option("sqlalchemy.url", to_async_dsn(TEST_DATABASE_URL))
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    """Moteur sur la base de test, tables vidées avant chaque test.

    `TRUNCATE` et non `DELETE` : remet aussi les séquences à zéro, pour que les
    identifiants du journal d'audit soient comparables d'un test à l'autre.
    """
    db = Database(migrated_database, pool_size=5)
    tables = ", ".join(f'"{SCHEMA}"."{table.name}"' for table in reversed(metadata.sorted_tables))
    async with db.transaction() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    try:
        yield db
    finally:
        await db.dispose()
