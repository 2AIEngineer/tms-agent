"""Tests de l'envoi des notifications, sur la chaîne réelle.

PostgreSQL réel, serveur MCP en mémoire branché sur le double du TMS, canaux
en mode console. Tout est vrai sauf le fournisseur de téléphonie, qui n'existe
pas encore — et c'est précisément ce qui permet de vérifier **ce qui serait
parti** plutôt qu'un simple statut en base.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from database.schema import notification_outbox, notifications
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from services.monitoring.alerts.service import AlertService
from services.monitoring.integrations.mcp_client import McpClient
from services.monitoring.notifications import NotificationService, OutboxRepository
from services.monitoring.rules.engine import evaluate_state_rules
from shared.channels import ConsoleChannel, SendResult
from shared.mapped_dto import NotificationChannel, NotificationStatus
from tests.factories import TMS_NOW, make_snapshot
from tests.fake_tms import FakeTms

pytestmark = pytest.mark.integration


@pytest.fixture
def tms() -> FakeTms:
    return FakeTms()


@pytest.fixture
def channels() -> dict[NotificationChannel, ConsoleChannel]:
    return {channel: ConsoleChannel(channel) for channel in NotificationChannel}


@pytest.fixture
async def mcp(tms: FakeTms):
    """Client MCP du monitoring, parlant au serveur en mémoire."""
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with McpClient(server) as client:
        yield client
    await context.aclose()


@pytest.fixture
def notifier(database, mcp, channels):
    def build(**overrides) -> NotificationService:
        return NotificationService(database, mcp, channels, retry_backoff_s=1, lease_s=60, **overrides)

    return build


async def count(database, table) -> int:
    async with database.connection() as connection:
        return int((await connection.execute(select(func.count()).select_from(table))).scalar_one())


async def rows(database, table):
    async with database.connection() as connection:
        return list((await connection.execute(select(table))).mappings())


async def raise_deviation_alert(database, shipped_rules, **overrides):
    """Crée une alerte de dérive, donc trois demandes d'envoi en file."""
    snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25, **overrides)
    service = AlertService(database)
    return await service.process_state_evaluations(
        snapshot, evaluate_state_rules(shipped_rules.rules, snapshot)
    )


class TestChaineComplete:
    async def test_alerte_puis_trois_notifications(self, database, shipped_rules, notifier, channels):
        """Chauffeur par appel, agent de suivi en in-app, superviseur par SMS."""
        report = await raise_deviation_alert(database, shipped_rules)
        assert report.created[0].notifications_enqueued == 3

        dispatch = await notifier().dispatch_once()

        assert dispatch.claimed == 3
        assert dispatch.delivered == 3
        assert dispatch.failed == 0

        # Ce qui serait réellement parti, canal par canal.
        assert [m.address for m in channels[NotificationChannel.CALL].sent] == ["+33600000001"]
        assert [m.address for m in channels[NotificationChannel.SMS].sent] == ["+33600000000"]
        assert channels[NotificationChannel.IN_APP].sent[0].address is None

    async def test_destinataires_resolus_dans_le_tms(self, database, shipped_rules, notifier):
        """F16 : les personnes ne sont pas figées à la détection."""
        await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        stored = {row["recipient_id"]: row for row in await rows(database, notifications)}
        assert set(stored) == {"drv_1", "usr_10", "usr_20"}
        assert stored["drv_1"]["channel"] == "call"
        assert stored["drv_1"]["management_level"] == "M+0"
        assert stored["usr_20"]["channel"] == "sms"
        assert all(row["status"] == "delivered" for row in stored.values())

    async def test_message_fige_a_la_detection(self, database, shipped_rules, notifier, channels):
        """Le texte parti est celui de l'alerte, pas une reconstruction."""
        await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        message = channels[NotificationChannel.SMS].sent[0]
        assert "AB-123-CD" in message.body
        assert "7.2 km" in message.body
        assert message.severity.value == "high"
        assert message.mission_id == "msn_1"

    async def test_demandes_closes_apres_traitement(self, database, shipped_rules, notifier):
        await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        statuses = {row["status"] for row in await rows(database, notification_outbox)}
        assert statuses == {"delivered"}
        assert await notifier().dispatch_once() == await notifier().dispatch_once()  # file vide

    async def test_alerte_dedupliquee_ne_renotifie_personne(self, database, shipped_rules, notifier):
        """F18 : aucune demande n'est mise en file pour une alerte supprimée."""
        await raise_deviation_alert(database, shipped_rules)
        await raise_deviation_alert(database, shipped_rules, observed_at=TMS_NOW + timedelta(minutes=1))

        await notifier().dispatch_once()

        assert await count(database, notifications) == 3  # et non 6


class TestEchecsDefinitifs:
    async def test_destinataire_sans_numero(self, database, shipped_rules, notifier, tms):
        """F17 : enregistré en échec avec sa raison, et non réessayé sans fin.

        Un superviseur sans téléphone est une correction à faire dans le TMS :
        aucune reprise ne fera apparaître le numéro.
        """
        tms.users_by_role["ops_supervisor"][0]["phone"] = None
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.delivered == 2
        assert dispatch.failed == 1
        failed = next(row for row in await rows(database, notifications) if row["status"] == "failed")
        assert failed["recipient_id"] == "usr_20"
        assert "phone" in failed["failure_reason"]
        # La demande est close : rien ne sera rejoué.
        sms_request = next(
            row for row in await rows(database, notification_outbox) if row["channel"] == "sms"
        )
        assert sms_request["status"] == "failed"

    async def test_role_sans_titulaire(self, database, shipped_rules, notifier, tms):
        tms.users_by_role["ops_agent"] = []
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.failed == 1
        assert dispatch.no_recipient
        in_app = next(row for row in await rows(database, notification_outbox) if row["channel"] == "in_app")
        assert in_app["status"] == "failed"
        assert "aucun destinataire" in in_app["failure_reason"]

    async def test_echec_de_notification_ne_touche_pas_lalerte(self, database, shipped_rules, notifier, tms):
        """F17 : un échec d'envoi ne recrée ni ne rouvre l'alerte."""
        from database.schema import alerts

        tms.users_by_role["ops_supervisor"] = []
        await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        stored = await rows(database, alerts)
        assert len(stored) == 1
        assert stored[0]["status"] == "open"

    async def test_canal_en_erreur_ne_tue_pas_le_worker(self, database, shipped_rules, notifier, channels):
        """Un fournisseur qui lève ne doit pas interrompre les autres envois."""

        async def boom(message, *, sent_at):
            raise RuntimeError("fournisseur SMS en vrac")

        channels[NotificationChannel.SMS].send = boom  # type: ignore[method-assign]
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.delivered == 2
        assert dispatch.failed == 1
        failed = next(row for row in await rows(database, notifications) if row["status"] == "failed")
        assert "fournisseur SMS en vrac" in failed["failure_reason"]

    async def test_echec_fournisseur_definitif(self, database, shipped_rules, notifier, channels):
        channels[NotificationChannel.SMS].fail_with = SendResult.permanent_failure(
            "console", "numéro invalide"
        )
        await raise_deviation_alert(database, shipped_rules)

        await notifier().dispatch_once()

        failed = next(row for row in await rows(database, notifications) if row["status"] == "failed")
        assert failed["failure_reason"] == "numéro invalide"


class TestIncidentsPassagers:
    async def test_tms_indisponible_reprogramme(self, database, shipped_rules, notifier, tms):
        """Une coupure de quelques secondes ne doit pas priver le superviseur.

        La demande reste `pending` : c'est ce qui la distingue d'un échec
        définitif et permet au cycle suivant de la reprendre.
        """
        tms.fail("/roles/ops_supervisor/users", 503)
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.rescheduled == 1
        sms_request = next(
            row for row in await rows(database, notification_outbox) if row["channel"] == "sms"
        )
        assert sms_request["status"] == "pending"
        assert sms_request["attempts"] == 1
        assert sms_request["available_at"] > sms_request["created_at"]  # repoussée

    async def test_panne_breve_absorbee_par_le_connecteur(self, database, shipped_rules, notifier, tms):
        """Un 503 isolé ne remonte jamais jusqu'à la file.

        Le connecteur TMS réessaie lui-même les erreurs transitoires : la file
        n'est sollicitée que si la panne dure. Les deux niveaux de reprise sont
        complémentaires, et c'est volontaire — reprogrammer une demande pour un
        hoquet de quelques millisecondes retarderait l'alerte inutilement.
        """
        tms.fail("/roles/ops_supervisor/users", 503, times=1)
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.rescheduled == 0
        assert dispatch.delivered == 3

    async def test_reprise_apres_retablissement(self, database, shipped_rules, notifier, tms):
        """Panne durable : reprogrammée, puis acheminée dès que le TMS répond."""
        tms.fail("/roles/ops_supervisor/users", 503)
        await raise_deviation_alert(database, shipped_rules)

        premier = await notifier().dispatch_once()
        assert premier.rescheduled == 1
        assert premier.delivered == 2

        tms.recover()
        # Le délai de reprise est ramené à zéro pour ne pas attendre réellement.
        async with database.transaction() as connection:
            await connection.execute(update(notification_outbox).values(available_at=func.now()))

        second = await notifier().dispatch_once()
        assert second.delivered == 1
        assert await count(database, notifications) == 3

    async def test_abandon_apres_le_plafond_de_tentatives(self, database, shipped_rules, notifier, tms):
        """F17 : passé le plafond, la demande est laissée à une reprise humaine."""
        tms.fail("/roles/ops_supervisor/users", 503)
        await raise_deviation_alert(database, shipped_rules)

        service = notifier(max_attempts=2)
        for _ in range(2):
            await service.dispatch_once()
            async with database.transaction() as connection:
                await connection.execute(update(notification_outbox).values(available_at=func.now()))

        sms_request = next(
            row for row in await rows(database, notification_outbox) if row["channel"] == "sms"
        )
        assert sms_request["status"] == "failed"
        assert sms_request["attempts"] == 2

    async def test_erreur_definitive_non_reprogrammee(self, database, shipped_rules, notifier, tms):
        """Un 403 sur l'annuaire ne se résoudra pas en réessayant."""
        tms.fail("/roles/ops_supervisor/users", 403)
        await raise_deviation_alert(database, shipped_rules)

        dispatch = await notifier().dispatch_once()

        assert dispatch.rescheduled == 0
        assert dispatch.abandoned == 1


class TestFileDAttente:
    async def test_bail_empeche_une_double_reclamation(self, database, shipped_rules, notifier, tms):
        """Une demande en cours d'acheminement ne doit pas être reprise."""
        tms.fail("/roles/ops_agent/users", 503)
        await raise_deviation_alert(database, shipped_rules)

        service = notifier()
        await service.dispatch_once()
        # Le bail court encore : plus rien d'éligible.
        assert (await service.dispatch_once()).claimed == 0

    async def test_expediteurs_concurrents_ne_doublonnent_pas(self, database, shipped_rules, notifier):
        """`SKIP LOCKED` : deux expéditeurs se partagent la file sans se bloquer."""
        await raise_deviation_alert(database, shipped_rules)

        premier, second = await asyncio.gather(notifier().dispatch_once(), notifier().dispatch_once())

        assert premier.claimed + second.claimed == 3
        assert await count(database, notifications) == 3

    async def test_enregistrement_idempotent(self, database, shipped_rules, notifier, channels):
        """Livraison « au moins une fois » : rejouer n'enregistre pas deux fois.

        Si le processus meurt entre l'envoi et l'enregistrement, le bail expire
        et la demande est reprise. L'index unique `(outbox_id, recipient_id)`
        garantit alors qu'une seule trace subsiste.
        """
        await raise_deviation_alert(database, shipped_rules)
        repository = OutboxRepository()

        async with database.transaction() as connection:
            entries = await repository.claim(connection, batch_size=10, lease_seconds=0)

        service = notifier()
        for _ in range(2):
            for entry in entries:
                await service._handle(entry, type(await service.dispatch_once())())

        assert await count(database, notifications) == 3

    async def test_profondeur_de_file(self, database, shipped_rules, notifier):
        """Compteur à surveiller : une file qui croît signale un envoi en panne."""
        await raise_deviation_alert(database, shipped_rules)
        service = notifier()

        assert await service.pending_count() == 3
        await service.dispatch_once()
        assert await service.pending_count() == 0

    async def test_lot_plafonne(self, database, shipped_rules, notifier):
        await raise_deviation_alert(database, shipped_rules)
        dispatch = await notifier(batch_size=2).dispatch_once()
        assert dispatch.claimed == 2


class TestTracabilite:
    async def test_envois_consultables_par_alerte(self, database, shipped_rules, notifier):
        """L'enquêteur en a besoin : savoir qui est déjà prévenu change ses recommandations."""
        report = await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        async with database.connection() as connection:
            sent = await OutboxRepository().notifications_for_alert(connection, report.created[0].alert_id)

        assert len(sent) == 3
        assert {item["channel"] for item in sent} == {"call", "in_app", "sms"}
        assert all(item["status"] == NotificationStatus.DELIVERED.value for item in sent)

    async def test_reference_du_fournisseur_conservee(self, database, shipped_rules, notifier):
        """Permet de relier un SMS reçu à l'alerte qui l'a provoqué."""
        await raise_deviation_alert(database, shipped_rules)
        await notifier().dispatch_once()

        stored = await rows(database, notifications)
        assert all(row["provider"] == "console" for row in stored)
        assert all(row["provider_reference"] for row in stored)
