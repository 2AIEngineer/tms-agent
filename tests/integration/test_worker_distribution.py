"""Tests de la répartition des TMS entre instances du worker.

L'unité de travail est le TMS. Deux propriétés en découlent, et ce sont elles
qui permettent de passer de un à vingt TMS sans changer de code :

  - **un TMS n'est surveillé que par une instance à la fois.** Deux instances
    évalueraient les mêmes missions et se disputeraient la file d'envoi ;
  - **plusieurs instances se répartissent les TMS sans coordinateur.** Le
    plafond `max_tms_per_worker` est ce qui transforme le verrou en
    répartiteur : sans lui, la première instance prendrait tout.

Les verrous étant tenus par la connexion PostgreSQL, un worker qui meurt les
libère et une autre instance reprend ses TMS — sans registre d'affectation à
maintenir.
"""

import asyncio

import pytest

from services.monitoring.config import MonitoringSettings
from services.monitoring.integrations.mcp_client import ConnectedTms
from services.monitoring.main import MonitoringWorker, tms_lock_key
from shared.utils.db import try_advisory_lock

pytestmark = pytest.mark.integration


def connected(*names: str) -> list[ConnectedTms]:
    return [ConnectedTms(tms_id=name, label=name, verdict="ok") for name in names]


def settings(**overrides) -> MonitoringSettings:
    return MonitoringSettings(**overrides)  # type: ignore[arg-type]


class TestCleDeVerrou:
    def test_stable_pour_un_meme_nom(self):
        """Le verrou doit survivre à un redémarrage : la clé ne dépend que du nom."""
        assert tms_lock_key(1000, "client_a") == tms_lock_key(1000, "client_a")

    def test_distincte_entre_deux_tms(self):
        """Une collision ferait surveiller un TMS par personne."""
        keys = {tms_lock_key(1000, f"client_{index}") for index in range(200)}
        assert len(keys) == 200

    def test_dans_les_bornes_dun_entier_postgres(self):
        """`pg_try_advisory_lock` prend un entier signé 64 bits ; on reste bien en deçà."""
        for name in ("a", "client_tres_long_nom_de_tms", "x_tms"):
            assert 0 <= tms_lock_key(0x746D7301, name) < 2**31


class TestPriseEnCharge:
    async def test_un_worker_prend_les_tms_disponibles(self, database):
        worker = MonitoringWorker(settings(max_tms_per_worker=8))
        available = connected("client_a", "client_b", "client_c")

        async with worker._claim(database, available) as owned:
            assert [item.tms.tms_id for item in owned] == ["client_a", "client_b", "client_c"]

    async def test_plafond_respecte(self, database):
        """Le plafond est ce qui laisse de quoi faire aux autres instances."""
        worker = MonitoringWorker(settings(max_tms_per_worker=2))
        available = connected("client_a", "client_b", "client_c", "client_d")

        async with worker._claim(database, available) as owned:
            assert len(owned) == 2
            assert [item.tms.tms_id for item in owned] == ["client_a", "client_b"]

    async def test_deux_instances_se_repartissent_sans_coordinateur(self, database):
        """Le cas qui compte : quatre TMS, deux instances, aucune négociation."""
        available = connected("client_a", "client_b", "client_c", "client_d")
        premiere = MonitoringWorker(settings(max_tms_per_worker=2))
        seconde = MonitoringWorker(settings(max_tms_per_worker=2))

        async with (
            premiere._claim(database, available) as owned_a,
            seconde._claim(database, available) as owned_b,
        ):
            pris_a = {item.tms.tms_id for item in owned_a}
            pris_b = {item.tms.tms_id for item in owned_b}

            assert len(pris_a) == 2
            assert len(pris_b) == 2
            # Aucun TMS surveillé deux fois, aucun oublié.
            assert pris_a.isdisjoint(pris_b)
            assert pris_a | pris_b == {"client_a", "client_b", "client_c", "client_d"}

    async def test_aucun_tms_disponible(self, database):
        """Toutes les instances servies : la suivante doit sortir, pas tourner à vide."""
        available = connected("client_a")
        occupante = MonitoringWorker(settings(max_tms_per_worker=4))
        tardive = MonitoringWorker(settings(max_tms_per_worker=4))

        async with (
            occupante._claim(database, available) as owned,
            tardive._claim(database, available) as rien,
        ):
            assert len(owned) == 1
            assert rien == []

    async def test_verrous_liberes_a_la_sortie(self, database):
        """Un worker arrêté rend ses TMS, qu'une autre instance peut reprendre."""
        available = connected("client_a", "client_b")
        worker = MonitoringWorker(settings(max_tms_per_worker=8))

        async with worker._claim(database, available) as owned:
            assert len(owned) == 2

        reprise = MonitoringWorker(settings(max_tms_per_worker=8))
        async with reprise._claim(database, available) as owned:
            assert len(owned) == 2

    async def test_verrou_tenu_par_une_connexion_dediee(self, database):
        """Sur une connexion du pool, le verrou tomberait au premier cycle.

        Ce test le vérifie de l'extérieur : pendant la prise en charge, le
        verrou est bien refusé à un tiers.
        """
        available = connected("client_a")
        worker = MonitoringWorker(settings(max_tms_per_worker=1))
        key = tms_lock_key(worker._settings.singleton_lock_key, "client_a")

        async with worker._claim(database, available) as owned:
            assert len(owned) == 1
            # Plusieurs allers-retours sur le pool entre-temps : le verrou tient.
            for _ in range(5):
                async with database.connection() as connection:
                    await connection.exec_driver_sql("select 1")
            async with database.engine.connect() as tiers:
                assert await try_advisory_lock(tiers, key) is False

    async def test_prises_concurrentes_ne_doublent_pas(self, database):
        """Trois instances démarrant en même temps sur deux TMS."""
        available = connected("client_a", "client_b")

        async def claim(worker: MonitoringWorker) -> set[str]:
            async with worker._claim(database, available) as owned:
                await asyncio.sleep(0.05)
                return {item.tms.tms_id for item in owned}

        resultats = await asyncio.gather(
            *(claim(MonitoringWorker(settings(max_tms_per_worker=1))) for _ in range(3))
        )
        pris = [tms for ensemble in resultats for tms in ensemble]

        assert sorted(pris) == ["client_a", "client_b"]  # chacun une fois


class TestDecouverte:
    async def test_allowlist_restreint_le_perimetre(self, database, monkeypatch):
        """Permet de dédier un conteneur à un client exigeant."""

        class FakeMcp:
            async def list_tms(self):
                return connected("client_a", "client_b", "client_c")

        worker = MonitoringWorker(settings(tms_allowlist=("client_b",)))
        retenus = await worker._discover(FakeMcp())  # type: ignore[arg-type]

        assert [tms.tms_id for tms in retenus] == ["client_b"]

    async def test_tms_degrade_signale_au_demarrage(self, caplog):
        """Pour qu'une absence d'alerte ne passe pas pour une absence d'anomalie."""
        import logging

        class FakeMcp:
            async def list_tms(self):
                return [
                    ConnectedTms(
                        tms_id="acme",
                        label="Acme",
                        verdict="degraded",
                        inoperative_rules=("deviation_persistent",),
                    )
                ]

        worker = MonitoringWorker(settings())
        with caplog.at_level(logging.WARNING, logger="services.monitoring.main"):
            retenus = await worker._discover(FakeMcp())  # type: ignore[arg-type]

        assert [tms.tms_id for tms in retenus] == ["acme"]
        message = caplog.records[0].getMessage()
        assert "dégradé" in message
        assert "deviation_persistent" in message

    async def test_serveur_mcp_injoignable_au_demarrage(self):
        """Sans la liste des TMS, il n'y a rien à surveiller : le worker sort."""
        from services.monitoring.integrations.mcp_client import McpCallFailed
        from shared.mapped_dto import ToolError, ToolErrorCode

        class BrokenMcp:
            async def list_tms(self):
                raise McpCallFailed(ToolError.of(ToolErrorCode.TMS_UNAVAILABLE, "serveur MCP injoignable"))

        worker = MonitoringWorker(settings())
        assert await worker._discover(BrokenMcp()) == []  # type: ignore[arg-type]
