"""Tests du cycle de vie des alertes, sur un PostgreSQL réel.

Ces tests ne peuvent pas être joués contre une base factice : ce qu'ils
vérifient — index unique partiel, verrou consultatif, atomicité d'une
transaction — est précisément ce qu'une fausse base ne reproduit pas.

Marqués `integration` : ils exigent le PostgreSQL natif et la base
`tms_agent_test_db` (créée par `scripts/infra.sh up`).
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from database.schema import alerts, audit_log, notification_outbox, processed_events
from services.monitoring.alerts.models import SuppressionReason
from services.monitoring.alerts.repository import AlertRepository
from services.monitoring.alerts.service import AlertService
from services.monitoring.rules.engine import evaluate_event_rules, evaluate_state_rules
from shared.mapped_dto import EventType, MissionStatus, Severity
from tests.factories import TMS_NOW, make_event, make_snapshot

pytestmark = pytest.mark.integration


async def count(database, table) -> int:
    async with database.connection() as connection:
        result = await connection.execute(select(func.count()).select_from(table))
        return int(result.scalar_one())


async def rows(database, table):
    async with database.connection() as connection:
        result = await connection.execute(select(table))
        return list(result.mappings())


def deviation_snapshot(**overrides):
    """Mission en dérive persistante : 7,2 km depuis 25 minutes."""
    return make_snapshot(deviation_km=7.2, deviation_minutes=25, **overrides)


async def run_state_cycle(service, rules, snapshot):
    return await service.process_state_evaluations(snapshot, evaluate_state_rules(rules, snapshot))


class TestCreation:
    async def test_alerte_creee_avec_ses_notifications(self, database, shipped_rules):
        service = AlertService(database)
        report = await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        assert len(report.created) == 1
        created = report.created[0]
        assert created.rule_id == "deviation_persistent"
        assert created.severity is Severity.HIGH
        # La règle notifie chauffeur, agent de suivi et superviseur.
        assert created.notifications_enqueued == 3

        stored = (await rows(database, alerts))[0]
        assert stored["status"] == "open"
        assert stored["rule_version"] == 2  # la version est conservée avec l'alerte
        assert stored["detected_at"] == TMS_NOW  # heure TMS, pas horloge locale
        assert stored["deduplication_key"] == "deviation:msn_1"

        queued = await rows(database, notification_outbox)
        assert [row["channel"] for row in queued] == ["call", "in_app", "sms"]
        assert [row["management_level"] for row in queued] == ["M+0", "M+1", "M+2"]
        assert all(row["status"] == "pending" for row in queued)
        assert all(row["alert_id"] == stored["id"] for row in queued)

    async def test_donnees_observees_serialisees(self, database, shipped_rules):
        """`observed_data` contient dates et énumérations : elles doivent survivre au JSONB."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        observed = (await rows(database, alerts))[0]["observed_data"]
        assert observed["deviation.current_offset_km"] == 7.2
        assert observed["mission.status"] == "in_progress"  # StrEnum -> chaîne
        assert isinstance(observed["deviation.vehicle_position"], dict)

    async def test_aucune_alerte_sans_declenchement(self, database, shipped_rules):
        service = AlertService(database)
        report = await run_state_cycle(service, shipped_rules.rules, make_snapshot())

        assert report.created == []
        assert await count(database, alerts) == 0
        assert await count(database, notification_outbox) == 0

    async def test_alertes_classees_par_gravite_pour_lenquete(self, database, shipped_rules):
        """F19 : avec un quota d'enquêtes par cycle, l'ordre décide de ce qui est analysé."""
        service = AlertService(database)
        snapshot = make_snapshot(
            deviation_km=7.2,
            deviation_minutes=25,  # high
            delay_minutes=90,  # medium
        )
        report = await run_state_cycle(service, shipped_rules.rules, snapshot)

        assert [created.severity for created in report.investigable] == [Severity.HIGH, Severity.MEDIUM]


class TestDeduplication:
    async def test_deuxieme_cycle_ne_cree_pas_de_doublon(self, database, shipped_rules):
        """F11 : au plus une alerte ouverte par clé, quel que soit le nombre de cycles."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        plus_tard = deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=1))
        report = await run_state_cycle(service, shipped_rules.rules, plus_tard)

        assert report.created == []
        assert [s.reason for s in report.suppressed] == [SuppressionReason.DUPLICATE]
        assert await count(database, alerts) == 1

    async def test_alerte_supprimee_ne_renotifie_personne(self, database, shipped_rules):
        """F18 : un doublon ne doit pas réveiller le chauffeur une seconde fois."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        await run_state_cycle(
            service, shipped_rules.rules, deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=1))
        )

        assert await count(database, notification_outbox) == 3  # et non 6

    async def test_dix_cycles_consecutifs_une_seule_alerte(self, database, shipped_rules):
        """Cas réel : une dérive dure vingt minutes, le cycle tourne toutes les 10 s."""
        service = AlertService(database)
        for minute in range(10):
            await run_state_cycle(
                service,
                shipped_rules.rules,
                deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=minute)),
            )

        assert await count(database, alerts) == 1
        assert await count(database, notification_outbox) == 3

    async def test_missions_distinctes_ne_se_dedupliquent_pas(self, database, shipped_rules):
        """La clé porte l'identifiant de mission : deux camions, deux alertes."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot(mission_id="msn_1"))
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot(mission_id="msn_2"))

        assert await count(database, alerts) == 2

    async def test_insertions_concurrentes_une_seule_alerte(self, database, shipped_rules):
        """Le test qui compte : deux écritures simultanées sur la même clé.

        Sans l'index unique partiel et le verrou consultatif, les deux
        constateraient l'absence d'alerte et en créeraient chacune une.
        """
        premier = AlertService(database)
        second = AlertService(database)
        snapshot = deviation_snapshot()

        rapports = await asyncio.gather(
            run_state_cycle(premier, shipped_rules.rules, snapshot),
            run_state_cycle(second, shipped_rules.rules, snapshot),
        )

        total_creees = sum(len(rapport.created) for rapport in rapports)
        total_supprimees = sum(len(rapport.suppressed) for rapport in rapports)
        assert total_creees == 1
        assert total_supprimees == 1
        assert await count(database, alerts) == 1
        assert await count(database, notification_outbox) == 3


class TestPlanificationPostgres:
    async def test_insertions_au_dela_du_seuil_de_plan_generique(self, database, shipped_rules):
        """Régression : l'insertion doit tenir après la 5e exécution préparée.

        PostgreSQL bascule d'un plan personnalisé à un plan **générique** après
        cinq exécutions d'une même requête préparée. Un `ON CONFLICT ... WHERE`
        dont le prédicat est un paramètre lié cesse alors d'être apparié à
        l'index partiel, et l'insertion lève. Le défaut est invisible sur
        quelques alertes et systématique en exploitation : d'où ce test, qui
        franchit délibérément le seuil sur une même connexion.
        """
        service = AlertService(database)
        for index in range(12):
            report = await run_state_cycle(
                service, shipped_rules.rules, deviation_snapshot(mission_id=f"msn_{index}")
            )
            assert len(report.created) == 1, f"insertion {index} en échec"

        assert await count(database, alerts) == 12

    async def test_cent_alertes_distinctes(self, database, shipped_rules):
        """Charge réaliste : une flotte de cent missions en dérive."""
        service = AlertService(database)
        for index in range(100):
            await run_state_cycle(
                service, shipped_rules.rules, deviation_snapshot(mission_id=f"flotte_{index}")
            )

        assert await count(database, alerts) == 100
        assert await count(database, notification_outbox) == 300


class TestCooldown:
    async def test_renaissance_pendant_le_cooldown_supprimee(self, database, shipped_rules):
        """Alerte close puis condition revenue : le cooldown de 30 min bloque la relance."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        # La dérive cesse : l'alerte est close (F13).
        retour_normal = make_snapshot(
            deviation_km=0.2, deviation_minutes=1, observed_at=TMS_NOW + timedelta(minutes=5)
        )
        await run_state_cycle(service, shipped_rules.rules, retour_normal)

        # Elle réapparaît 10 minutes plus tard, sous le cooldown de 30 minutes.
        rechute = deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=15))
        report = await run_state_cycle(service, shipped_rules.rules, rechute)

        assert report.created == []
        assert [s.reason for s in report.suppressed] == [SuppressionReason.COOLDOWN]
        assert await count(database, alerts) == 1

    async def test_renaissance_apres_le_cooldown_recree(self, database, shipped_rules):
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        await run_state_cycle(
            service,
            shipped_rules.rules,
            make_snapshot(deviation_km=0.2, deviation_minutes=1, observed_at=TMS_NOW + timedelta(minutes=5)),
        )

        rechute = deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=45))
        report = await run_state_cycle(service, shipped_rules.rules, rechute)

        assert len(report.created) == 1
        assert await count(database, alerts) == 2

    async def test_cooldown_mesure_en_heure_tms(self, database, shipped_rules):
        """F11 : le cooldown se juge sur l'horloge du TMS, que le simulateur accélère."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        await run_state_cycle(
            service,
            shipped_rules.rules,
            make_snapshot(deviation_km=0.2, deviation_minutes=1, observed_at=TMS_NOW + timedelta(minutes=1)),
        )

        # Le TMS a avancé de deux heures en temps accéléré : le cooldown est écoulé.
        report = await run_state_cycle(
            service, shipped_rules.rules, deviation_snapshot(observed_at=TMS_NOW + timedelta(hours=2))
        )
        assert len(report.created) == 1

    async def test_sans_cooldown_relance_immediate(self, database, shipped_rules):
        """`arrival_out_of_zone` a un cooldown nul : chaque événement compte."""
        service = AlertService(database)
        snapshot = make_snapshot()

        for index in range(2):
            event = make_event(
                event_id=f"EVT-{index}",
                event_type=EventType.ARRIVED_DELIVERY,
                distance_to_target_km=3.4,
            )
            report = await service.process_event_evaluations(
                snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
            )
            assert len(report.created) == 1

        assert await count(database, alerts) == 2


class TestClotureAutomatique:
    async def test_condition_disparue_clot_lalerte(self, database, shipped_rules):
        """F13 : le camion revient sur son itinéraire, l'alerte se ferme seule."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        retour = make_snapshot(
            deviation_km=0.3, deviation_minutes=1, observed_at=TMS_NOW + timedelta(minutes=10)
        )
        report = await run_state_cycle(service, shipped_rules.rules, retour)

        assert len(report.resolved) == 1
        stored = (await rows(database, alerts))[0]
        assert stored["status"] == "resolved"
        assert stored["resolution_reason"] == "condition_cleared"
        assert stored["resolved_at"] == TMS_NOW + timedelta(minutes=10)

    async def test_instantane_partiel_ne_clot_rien(self, database, shipped_rules):
        """F14 : un appel TMS en échec ressemble, vu du moteur, à une condition disparue."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        partiel = make_snapshot(
            with_deviation=False,
            snapshot_complete=False,
            observed_at=TMS_NOW + timedelta(minutes=10),
        )
        report = await run_state_cycle(service, shipped_rules.rules, partiel)

        assert report.resolved == []
        assert (await rows(database, alerts))[0]["status"] == "open"

    async def test_alerte_devenement_non_close_mission_active(self, database, shipped_rules):
        """F14 : un incident signalé par le chauffeur ne cesse pas d'avoir eu lieu."""
        service = AlertService(database)
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.DRIVER_MESSAGE,
            payload={"severity": "critical", "text": "Accident"},
        )
        await service.process_event_evaluations(
            snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
        )

        # Plusieurs cycles d'état passent, sans aucun incident.
        for minute in (5, 10, 15):
            await run_state_cycle(
                service, shipped_rules.rules, make_snapshot(observed_at=TMS_NOW + timedelta(minutes=minute))
            )

        assert (await rows(database, alerts))[0]["status"] == "open"

    async def test_mission_terminee_clot_tout(self, database, shipped_rules):
        """F13 : mission livrée, plus rien à surveiller — alertes d'événement comprises."""
        service = AlertService(database)
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.DRIVER_MESSAGE, payload={"severity": "critical", "text": "Accident"}
        )
        await service.process_event_evaluations(
            snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
        )
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        assert await count(database, alerts) == 2

        terminee = make_snapshot(status=MissionStatus.COMPLETED, observed_at=TMS_NOW + timedelta(hours=1))
        report = await run_state_cycle(service, shipped_rules.rules, terminee)

        assert len(report.resolved) == 2
        assert all(row["resolution_reason"] == "mission_ended" for row in await rows(database, alerts))

    async def test_cloture_idempotente(self, database, shipped_rules):
        """Deux cycles de clôture d'affilée ne doivent pas rouvrir ni recompter."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        retour = make_snapshot(deviation_km=0.3, observed_at=TMS_NOW + timedelta(minutes=10))

        premier = await run_state_cycle(service, shipped_rules.rules, retour)
        second = await run_state_cycle(service, shipped_rules.rules, retour)

        assert len(premier.resolved) == 1
        assert second.resolved == []

    async def test_autres_regles_non_affectees(self, database, shipped_rules):
        """La clôture ne vise que les alertes de la règle devenue fausse."""
        service = AlertService(database)
        snapshot = make_snapshot(deviation_km=7.2, deviation_minutes=25, delay_minutes=90)
        await run_state_cycle(service, shipped_rules.rules, snapshot)
        assert await count(database, alerts) == 2

        # La dérive cesse, le retard demeure.
        suite = make_snapshot(
            deviation_km=0.2,
            deviation_minutes=1,
            delay_minutes=90,
            observed_at=TMS_NOW + timedelta(minutes=10),
        )
        await run_state_cycle(service, shipped_rules.rules, suite)

        statuts = {row["rule_id"]: row["status"] for row in await rows(database, alerts)}
        assert statuts == {"deviation_persistent": "resolved", "eta_delay": "open"}


class TestEvenements:
    async def test_evenement_traite_une_seule_fois(self, database, shipped_rules):
        """F7 : le TMS renvoie l'historique complet à chaque appel."""
        service = AlertService(database)
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.DRIVER_MESSAGE, payload={"severity": "critical", "text": "Accident"}
        )

        premiers = await service.filter_new_events("x_tms", [event])
        assert premiers == [event]
        await service.process_event_evaluations(
            snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
        )

        assert await service.filter_new_events("x_tms", [event]) == []
        assert await count(database, processed_events) == 1

    async def test_evenement_sans_alerte_marque_traite(self, database, shipped_rules):
        """Un événement anodin ne doit pas être réévalué indéfiniment."""
        service = AlertService(database)
        snapshot = make_snapshot()
        event = make_event(event_type=EventType.LOADING_COMPLETED)

        await service.process_event_evaluations(
            snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
        )

        assert await count(database, alerts) == 0
        assert await service.filter_new_events("x_tms", [event]) == []

    async def test_marquage_et_alertes_dans_la_meme_transaction(self, database, shipped_rules, monkeypatch):
        """Si l'écriture échoue, l'événement reste à traiter plutôt que d'être perdu."""
        service = AlertService(database)
        snapshot = make_snapshot()
        event = make_event(
            event_type=EventType.DRIVER_MESSAGE, payload={"severity": "critical", "text": "Accident"}
        )

        async def boom(*args, **kwargs):
            raise RuntimeError("panne au milieu de la transaction")

        monkeypatch.setattr(service._repository, "mark_event_processed", boom)

        with pytest.raises(RuntimeError):
            await service.process_event_evaluations(
                snapshot, event, evaluate_event_rules(shipped_rules.rules, snapshot, event)
            )

        assert await count(database, alerts) == 0  # l'alerte a été annulée avec le reste
        assert await service.filter_new_events("x_tms", [event]) == [event]  # l'événement sera repris


class TestJournalDaudit:
    async def test_creation_tracee(self, database, shipped_rules):
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        entries = await rows(database, audit_log)
        created = next(entry for entry in entries if entry["action"] == "alert_created")
        assert created["rule_id"] == "deviation_persistent"
        assert created["detail"]["notifications_enqueued"] == 3
        assert created["occurred_at"] == TMS_NOW

    async def test_suppression_tracee_avec_sa_raison(self, database, shipped_rules):
        """F11 : sans cette trace, une alerte attendue et jamais reçue est inexplicable."""
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        await run_state_cycle(
            service, shipped_rules.rules, deviation_snapshot(observed_at=TMS_NOW + timedelta(minutes=1))
        )

        entries = await rows(database, audit_log)
        suppressed = next(entry for entry in entries if entry["action"] == "alert_suppressed")
        assert suppressed["detail"]["reason"] == "duplicate"

    async def test_cloture_tracee(self, database, shipped_rules):
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())
        await run_state_cycle(
            service,
            shipped_rules.rules,
            make_snapshot(deviation_km=0.2, observed_at=TMS_NOW + timedelta(minutes=10)),
        )

        actions = [entry["action"] for entry in await rows(database, audit_log)]
        assert "alert_resolved" in actions


class TestJournalHeterogene:
    """Une création et une suppression dans le même cycle, sur la même mission.

    Défaut trouvé en conditions réelles, sur une démonstration longue : une
    insertion multi-lignes exige que toutes les lignes portent les mêmes clés,
    et une entrée `alert_created` (qui porte un `alert_id`) voisine d'une
    entrée `alert_suppressed` (qui n'en a pas) faisait échouer l'insertion
    entière. Le journal étant écrit dans la transaction de l'alerte, c'est
    **l'alerte qui était perdue** — le cycle rapportait seulement « mission
    illisible », et une alerte d'immobilisation est restée invisible une heure.
    """

    async def test_lalerte_survit_a_un_journal_de_formes_melangees(self, database, shipped_rules):
        service = AlertService(database)
        # Premier cycle : la dérive crée son alerte.
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        # Second cycle : la dérive est un doublon (suppression, sans alert_id)
        # et le retard en crée une (création, avec alert_id).
        report = await run_state_cycle(
            service,
            shipped_rules.rules,
            make_snapshot(
                deviation_km=7.2,
                deviation_minutes=25,
                delay_minutes=60,
                observed_at=TMS_NOW + timedelta(minutes=1),
            ),
        )

        assert [created.rule_id for created in report.created] == ["eta_delay"]
        assert [item.rule_id for item in report.suppressed] == ["deviation_persistent"]
        # L'alerte de retard existe réellement : c'est elle qui disparaissait.
        stored = {entry["rule_id"] for entry in await rows(database, alerts)}
        assert stored == {"deviation_persistent", "eta_delay"}
        actions = {entry["action"] for entry in await rows(database, audit_log)}
        assert {"alert_created", "alert_suppressed"} <= actions

    async def test_entrees_de_formes_differentes_toutes_ecrites(self, database):
        """Le dépôt regroupe par forme, et l'ordre d'écriture est conservé."""
        repository = AlertRepository()
        entries: list[dict[str, object]] = [
            {
                "action": "alert_created",
                "tms_id": "x_tms",
                "mission_id": "msn_1",
                "alert_id": uuid.uuid4(),
                "rule_id": "deviation_persistent",
                "detail": {"severity": "high"},
                "occurred_at": TMS_NOW,
            },
            {
                "action": "alert_suppressed",
                "tms_id": "x_tms",
                "mission_id": "msn_1",
                "rule_id": "eta_delay",
                "detail": {"reason": "duplicate"},
                "occurred_at": TMS_NOW,
            },
            {
                "action": "rule_logged",
                "tms_id": "x_tms",
                "mission_id": "msn_1",
                "rule_id": "arrival_out_of_zone",
                "detail": {},
                "occurred_at": TMS_NOW,
            },
        ]

        async with database.transaction() as connection:
            await repository.append_audit(connection, entries)

        written = sorted(await rows(database, audit_log), key=lambda entry: entry["id"])
        assert [entry["action"] for entry in written] == [
            "alert_created",
            "alert_suppressed",
            "rule_logged",
        ]
        # `actor` tient sa valeur du défaut côté base : les clés absentes ne
        # peuvent donc pas être comblées par `None`.
        assert all(entry["actor"] == "monitoring_worker" for entry in written)


class TestResilience:
    async def test_mission_sans_vehicule_ne_bloque_pas_le_cycle(self, database, shipped_rules):
        """F4 : un instantané incomplet est signalé, il n'interrompt pas la surveillance."""
        service = AlertService(database)
        snapshot = deviation_snapshot().model_copy(update={"vehicle": None})

        report = await run_state_cycle(service, shipped_rules.rules, snapshot)
        assert len(report.created) == 1

    async def test_mission_annulee_clot_sans_evaluer(self, database, shipped_rules):
        service = AlertService(database)
        await run_state_cycle(service, shipped_rules.rules, deviation_snapshot())

        annulee = make_snapshot(
            status=MissionStatus.CANCELLED,
            deviation_km=7.2,
            deviation_minutes=25,
            observed_at=TMS_NOW + timedelta(minutes=10),
        )
        report = await run_state_cycle(service, shipped_rules.rules, annulee)

        assert report.created == []
        assert len(report.resolved) == 1

    async def test_vingt_missions_en_parallele(self, database, shipped_rules):
        """Le cycle traite les missions en parallèle : chacune doit rester isolée."""
        service = AlertService(database)
        snapshots = [deviation_snapshot(mission_id=f"msn_{index}") for index in range(20)]

        await asyncio.gather(*(run_state_cycle(service, shipped_rules.rules, s) for s in snapshots))

        assert await count(database, alerts) == 20
        assert await count(database, notification_outbox) == 60
