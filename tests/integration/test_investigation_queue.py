"""Tests de la réclamation des enquêtes.

Ce qui se joue ici : **une alerte n'est enquêtée qu'une fois**, et les plus
graves d'abord (F19). Il n'y a pas de table de file d'attente — la file est la
table des alertes filtrée par « aucune enquête rattachée » — et la réclamation
est une insertion dont l'index unique assure l'exclusivité.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from database.schema import alerts, investigations
from services.agents.investigator.repository import InvestigationRepository
from services.monitoring.alerts.repository import AlertRepository
from shared.mapped_dto import (
    Alert,
    AlertStatus,
    InvestigationStatus,
    ResolutionReason,
    Severity,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def repository() -> InvestigationRepository:
    return InvestigationRepository()


async def seed_alert(
    database,
    severity: Severity,
    *,
    mission_id: str,
    tms_id: str = "x_tms",
    detected_at: datetime | None = None,
) -> uuid.UUID:
    alert = Alert(
        tms_id=tms_id,
        mission_id=mission_id,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=severity,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté.",
        deduplication_key=f"deviation:{tms_id}:{mission_id}",
        detected_at=detected_at or datetime.now(UTC),
    )
    async with database.transaction() as connection:
        inserted = await AlertRepository().insert_alert(connection, alert)
    assert inserted is not None
    return inserted


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


class TestEnquetesSautees:
    """Une enquête sautée doit pouvoir être reprise quand le modèle revient.

    Mesuré en conditions réelles : le palier gratuit du fournisseur a répondu
    429 pendant quelques minutes, et toutes les alertes de cette fenêtre se
    retrouvaient privées d'analyse **définitivement** — la file étant la table
    des alertes filtrée par « aucune enquête rattachée », une ligne `skipped`
    suffisait à les écarter. Rien ne le signalait : l'alerte, elle, existait.
    """

    async def test_enquete_sautee_reprise(self, database, repository):
        alert_id = await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            await repository.fail(
                connection,
                claimed[0],
                "quota d'inférence épuisé",
                status=InvestigationStatus.SKIPPED,
            )

        async with database.transaction() as connection:
            reprises = await repository.reclaim_skipped(
                connection,
                older_than=datetime.now(UTC) + timedelta(seconds=1),
                max_attempts=3,
            )

        assert reprises == claimed
        async with database.connection() as connection:
            row = (
                (
                    await connection.execute(
                        select(investigations.c.status, investigations.c.attempts, investigations.c.error)
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == InvestigationStatus.RUNNING.value
        assert row["attempts"] == 1
        # Le motif de la tentative précédente est effacé : il ne décrit plus
        # l'état courant, et le laisser ferait croire à un échec persistant.
        assert row["error"] is None
        assert alert_id is not None

    async def test_alerte_close_non_reprise(self, database, repository):
        """Analyser une alerte close depuis une heure n'apprend plus rien."""
        alert_id = await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            await repository.fail(connection, claimed[0], "modèle absent", status=InvestigationStatus.SKIPPED)
            await AlertRepository().resolve(
                connection,
                [alert_id],
                resolved_at=datetime.now(UTC),
                reason=ResolutionReason.CONDITION_CLEARED,
            )

        async with database.transaction() as connection:
            reprises = await repository.reclaim_skipped(
                connection,
                older_than=datetime.now(UTC) + timedelta(seconds=1),
                max_attempts=3,
            )

        assert reprises == []

    async def test_reprise_bornee_par_les_tentatives(self, database, repository):
        """Un modèle durablement absent ne doit pas faire boucler la file."""
        await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)

        for _ in range(4):
            async with database.transaction() as connection:
                await repository.fail(
                    connection, claimed[0], "modèle absent", status=InvestigationStatus.SKIPPED
                )
                reprises = await repository.reclaim_skipped(
                    connection,
                    older_than=datetime.now(UTC) + timedelta(seconds=1),
                    max_attempts=3,
                )

        assert reprises == []
        async with database.connection() as connection:
            attempts = (await connection.execute(select(investigations.c.attempts))).scalar_one()
        assert attempts == 3


class TestReclamation:
    async def test_alerte_eligible_reclamee(self, database, repository):
        await seed_alert(database, Severity.HIGH, mission_id="msn_1")

        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert len(claimed) == 1
        assert await count(database, investigations) == 1

    async def test_sous_le_seuil_ignoree(self, database, repository):
        """F19 : seules les alertes de sévérité suffisante sont enquêtées."""
        await seed_alert(database, Severity.LOW, mission_id="msn_1")

        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert claimed == []
        assert await count(database, investigations) == 0

    async def test_les_plus_graves_dabord(self, database, repository):
        """Avec un quota par passage, l'ordre décide de ce qui est analysé."""
        for index, severity in enumerate(
            [Severity.MEDIUM, Severity.CRITICAL, Severity.MEDIUM, Severity.HIGH]
        ):
            await seed_alert(database, severity, mission_id=f"msn_{index}")

        async with database.transaction() as connection:
            await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=2)

        async with database.connection() as connection:
            retenues = (await connection.execute(select(investigations.c.severity))).scalars().all()
        assert sorted(retenues) == ["critical", "high"]

    async def test_quota_par_passage(self, database, repository):
        """F19 : au plus quatre par passage, pour que la détection ne soit pas noyée."""
        for index in range(10):
            await seed_alert(database, Severity.HIGH, mission_id=f"msn_{index}")

        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert len(claimed) == 4
        assert await count(database, investigations) == 4

    async def test_une_alerte_nest_enquetee_quune_fois(self, database, repository):
        """Deux passages consécutifs ne doivent pas rouvrir la même enquête."""
        await seed_alert(database, Severity.HIGH, mission_id="msn_1")

        async with database.transaction() as connection:
            premier = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)
        async with database.transaction() as connection:
            second = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert len(premier) == 1
        assert second == []

    async def test_instances_concurrentes_ne_doublonnent_pas(self, database, repository):
        """Le test qui compte : deux services agent réclamant en même temps.

        Aucun verrou ni `SKIP LOCKED` : l'index unique sur `alert_id` suffit,
        la seconde insertion étant rejetée sans erreur.
        """
        for index in range(4):
            await seed_alert(database, Severity.HIGH, mission_id=f"msn_{index}")

        async def claim() -> list[uuid.UUID]:
            async with database.transaction() as connection:
                return await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=2)

        premier, second = await asyncio.gather(claim(), claim())

        assert set(premier).isdisjoint(set(second))
        assert len(premier) + len(second) == 4
        assert await count(database, investigations) == 4

    async def test_alerte_close_non_enquetee(self, database, repository):
        """Une alerte déjà résolue n'a plus besoin d'explication."""
        alert_id = await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            await AlertRepository().resolve(
                connection,
                [alert_id],
                resolved_at=datetime.now(UTC),
                reason=ResolutionReason.CONDITION_CLEARED,
            )

        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert claimed == []

    async def test_isolee_par_tms(self, database, repository):
        """Deux TMS, deux alertes, deux enquêtes distinctes."""
        for tms_id in ("client_a", "client_b"):
            await seed_alert(database, Severity.HIGH, mission_id="msn_1", tms_id=tms_id)

        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)

        assert len(claimed) == 2
        async with database.connection() as connection:
            provenances = (await connection.execute(select(investigations.c.tms_id))).scalars().all()
        assert set(provenances) == {"client_a", "client_b"}


class TestContexte:
    async def test_chargement_de_lalerte_et_des_notifications(self, database, repository):
        """F21 : l'enquêteur reçoit l'alerte et qui a déjà été prévenu."""
        alert_id = await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            await connection.execute(
                text(
                    "insert into orchestrator.notification_outbox "
                    "(id, alert_id, tms_id, role, management_level, channel) "
                    "values (gen_random_uuid(), :alert, 'x_tms', 'ops_supervisor', 'M+2', 'sms')"
                ),
                {"alert": alert_id},
            )
            outbox_id = (
                await connection.execute(text("select id from orchestrator.notification_outbox limit 1"))
            ).scalar_one()
            await connection.execute(
                text(
                    "insert into orchestrator.notifications "
                    "(id, outbox_id, alert_id, channel, management_level, recipient_id, "
                    " recipient_role, status, sent_at) "
                    "values (gen_random_uuid(), :outbox, :alert, 'sms', 'M+2', 'usr_20', "
                    " 'ops_supervisor', 'delivered', now())"
                ),
                {"outbox": outbox_id, "alert": alert_id},
            )
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)

        async with database.connection() as connection:
            context = await repository.load(connection, claimed[0])

        assert context is not None
        assert context.alert.rule_id == "deviation_persistent"
        assert context.alert.severity is Severity.HIGH
        assert context.alert.status is AlertStatus.OPEN
        assert len(context.notifications_sent) == 1
        assert "ops_supervisor" in context.notifications_sent[0]
        assert "delivered" in context.notifications_sent[0]

    async def test_enquete_inexistante(self, database, repository):
        async with database.connection() as connection:
            assert await repository.load(connection, uuid.uuid4()) is None


class TestCycleDeVie:
    async def test_enquete_abandonnee_reprise(self, database, repository):
        """Une instance tuée en cours d'enquête laisse une ligne que personne
        ne reprendrait : sans cette reprise, l'alerte resterait sans analyse
        pour toujours, et rien ne le signalerait."""
        await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            # Le bail est antidaté, comme si l'instance était morte.
            await connection.execute(
                text("update orchestrator.investigations set claimed_at = now() - interval '1 hour'")
            )

        async with database.transaction() as connection:
            reprises = await repository.reclaim_stale(
                connection,
                older_than=datetime.now(UTC) - timedelta(minutes=10),
                max_attempts=3,
            )

        assert len(reprises) == 1
        async with database.connection() as connection:
            attempts = (await connection.execute(select(investigations.c.attempts))).scalar_one()
        assert attempts == 1

    async def test_plafond_de_tentatives(self, database, repository):
        """Au-delà, l'alerte reste sans analyse — préférable à une boucle d'échecs."""
        await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            await connection.execute(
                text(
                    "update orchestrator.investigations "
                    "set claimed_at = now() - interval '1 hour', attempts = 3"
                )
            )

        async with database.transaction() as connection:
            reprises = await repository.reclaim_stale(
                connection,
                older_than=datetime.now(UTC) - timedelta(minutes=10),
                max_attempts=3,
            )

        assert reprises == []

    async def test_echec_denquete_ne_touche_pas_lalerte(self, database, repository):
        """L'alerte reste valable et notifiée : seule l'analyse manque."""
        alert_id = await seed_alert(database, Severity.HIGH, mission_id="msn_1")
        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            await repository.fail(
                connection,
                claimed[0],
                "LLM indisponible",
                status=InvestigationStatus.SKIPPED,
            )

        async with database.connection() as connection:
            stored = (
                await connection.execute(select(alerts.c.status).where(alerts.c.id == alert_id))
            ).scalar_one()
            etat = await repository.result_for_alert(connection, alert_id)

        assert stored == "open"
        assert etat is not None
        assert etat["status"] == "skipped"
        assert etat["result"] is None

    async def test_profondeur_de_file(self, database, repository):
        """Compteur à surveiller : s'il croît, l'enquête ne suit pas la détection."""
        for index in range(3):
            await seed_alert(database, Severity.HIGH, mission_id=f"msn_{index}")

        async with database.transaction() as connection:
            await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=3)
        async with database.connection() as connection:
            assert await repository.pending_count(connection) == 3
