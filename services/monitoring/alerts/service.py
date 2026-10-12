"""Cycle de vie des alertes : création, déduplication, cooldown, clôture.

Le moteur de règles dit ce qu'il observe ; ce service décide ce qui devient une
alerte. La frontière est volontaire : les règles restent testables sans base de
données, et toute la logique d'unicité tient ici, en un seul endroit.

Garanties portées par ce module :

  - **Pas de doublon** (F11) : au plus une alerte ouverte par clé de
    déduplication, et un cooldown en heure TMS. Les deux sont tranchés dans une
    même transaction, sous verrou consultatif sur la clé.
  - **Atomicité** : alerte, notifications à envoyer et marquage de l'événement
    traité sont écrits ensemble, ou rien ne l'est (F7, F17).
  - **Pas de faux « résolu »** (F14) : une alerte d'état n'est close que sur un
    instantané complet ; une alerte d'événement ne se clôt jamais d'elle-même
    tant que la mission est active.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from shared.mapped_dto import (
    Alert,
    MissionEvent,
    MissionSnapshot,
    MissionStatus,
    ResolutionReason,
    RuleAction,
    RuleEvaluation,
)
from shared.utils.db import Database, advisory_xact_lock

from .models import (
    AlertCreated,
    AlertCycleReport,
    AlertResolved,
    AlertSuppressed,
    RuleLogged,
    SuppressionReason,
    alert_fingerprint,
)
from .repository import AlertRepository, OpenAlert

logger = logging.getLogger(__name__)

# Statuts pour lesquels une mission est encore surveillée. Hors de cette liste,
# ses alertes d'état n'ont plus d'objet et sont closes (F13).
ACTIVE_STATUSES = frozenset({MissionStatus.PLANNED, MissionStatus.IN_PROGRESS})


class AlertService:
    def __init__(self, database: Database, repository: AlertRepository | None = None) -> None:
        self._database = database
        self._repository = repository or AlertRepository()

    # --- Règles d'état -------------------------------------------------------

    async def process_state_evaluations(
        self,
        snapshot: MissionSnapshot,
        evaluations: Sequence[RuleEvaluation],
    ) -> AlertCycleReport:
        """Applique les décisions d'un cycle pour une mission.

        Une seule transaction pour toute la mission : le bilan du cycle décrit
        alors un état cohérent de la base, et non un instantané pris au milieu
        d'écritures partielles.
        """
        report = AlertCycleReport(tms_id=snapshot.tms_id, mission_id=snapshot.mission_id)
        tms_now = snapshot.observed_at

        async with self._database.transaction() as connection:
            if snapshot.status not in ACTIVE_STATUSES:
                # Mission terminée ou annulée : plus rien à détecter, tout est
                # clos. On sort avant d'évaluer quoi que ce soit d'autre.
                await self._resolve_mission_ended(connection, snapshot, report)
                return report

            for evaluation in evaluations:
                if evaluation.matched:
                    await self._apply_match(connection, snapshot, evaluation, report, event=None)
                else:
                    await self._apply_clearance(connection, snapshot, evaluation, report)

            await self._audit(connection, report, tms_now)

        return report

    # --- Règles d'événement --------------------------------------------------

    async def process_event_evaluations(
        self,
        snapshot: MissionSnapshot,
        event: MissionEvent,
        evaluations: Sequence[RuleEvaluation],
    ) -> AlertCycleReport:
        """Traite un événement nouveau et le marque traité dans la même transaction.

        L'ordre compte : l'événement n'est marqué traité qu'**après** l'écriture
        de ses alertes. Si la transaction échoue, rien n'est marqué et le cycle
        suivant le reprendra — plutôt que de le perdre définitivement (F7).
        """
        report = AlertCycleReport(tms_id=snapshot.tms_id, mission_id=snapshot.mission_id)
        tms_now = snapshot.observed_at

        async with self._database.transaction() as connection:
            for evaluation in evaluations:
                if evaluation.matched:
                    await self._apply_match(connection, snapshot, evaluation, report, event=event)

            await self._repository.mark_event_processed(
                connection,
                event,
                tms_id=snapshot.tms_id,
                processed_at=tms_now,
                alerts_created=len(report.created),
            )
            await self._audit(connection, report, tms_now)

        return report

    async def filter_new_events(
        self,
        tms_id: str,
        events: Sequence[MissionEvent],
    ) -> list[MissionEvent]:
        """Ne garde que les événements jamais traités, dans leur ordre d'origine.

        La provenance est un paramètre et non un champ de `MissionEvent` : un
        événement est toujours obtenu dans le contexte de sa mission, et
        alourdir le contrat pivot pour le redire n'apporterait rien.
        """
        if not events:
            return []
        async with self._database.connection() as connection:
            new_ids = await self._repository.unprocessed_event_ids(
                connection, tms_id, [event.id for event in events]
            )
        return [event for event in events if event.id in new_ids]

    # --- Décisions élémentaires ----------------------------------------------

    async def _apply_match(
        self,
        connection: AsyncConnection,
        snapshot: MissionSnapshot,
        evaluation: RuleEvaluation,
        report: AlertCycleReport,
        *,
        event: MissionEvent | None,
    ) -> None:
        # F12 : ces actions sont journalisées, sans alerte ni notification.
        if evaluation.action is not RuleAction.EMIT_ALERT:
            action = evaluation.action.value if evaluation.action else "unknown"
            report.logged.append(RuleLogged(rule_id=evaluation.rule_id, action=action))
            return

        if evaluation.severity is None or evaluation.title is None or evaluation.message is None:
            # Ne peut se produire que si une règle déclenche sans gabarit, ce
            # que le schéma interdit. On refuse d'inventer un contenu d'alerte.
            logger.error("Évaluation déclenchée incomplète, ignorée : règle %s", evaluation.rule_id)
            return

        key = alert_fingerprint(evaluation)
        tms_now = evaluation.evaluated_at

        # Sérialise les décisions portant sur cette clé : les contrôles et
        # l'insertion deviennent indivisibles.
        # Le verrou porte sur la clé **et** la provenance : deux TMS traitant une
        # mission du même identifiant ne doivent pas s'attendre l'un l'autre, et
        # la portée du verrou doit coïncider avec celle de l'unicité.
        await advisory_xact_lock(connection, f"{snapshot.tms_id}:{key}")

        # L'ordre des deux contrôles est significatif. Une alerte encore
        # ouverte est un doublon : l'opérateur la voit déjà dans sa console.
        # Le cooldown ne concerne que le cas suivant — la condition avait
        # disparu, l'alerte avait été close, et le problème revient trop vite.
        # Les intervertir annoncerait « cooldown » là où il faut lire
        # « doublon », et rendrait le bilan de cycle trompeur.
        if await self._repository.has_open_alert(connection, snapshot.tms_id, key):
            report.suppressed.append(self._suppressed(evaluation, key, SuppressionReason.DUPLICATE))
            return

        if await self._repository.is_within_cooldown(
            connection, snapshot.tms_id, key, evaluation.cooldown_minutes, tms_now
        ):
            report.suppressed.append(self._suppressed(evaluation, key, SuppressionReason.COOLDOWN))
            return

        alert = Alert(
            id=uuid.uuid4(),
            tms_id=snapshot.tms_id,
            mission_id=snapshot.mission_id,
            rule_id=evaluation.rule_id,
            rule_version=evaluation.rule_version,
            severity=evaluation.severity,
            category=evaluation.category or "uncategorized",
            title=evaluation.title,
            message=evaluation.message,
            observed_data=evaluation.observed_data,
            deduplication_key=key,
            cooldown_minutes=evaluation.cooldown_minutes,
            auto_resolve=evaluation.auto_resolve,
            event_id=event.id if event else None,
            detected_at=tms_now,
        )

        alert_id = await self._repository.insert_alert(connection, alert)
        if alert_id is None:
            # Filet de sécurité : l'index unique partiel a tranché, une alerte
            # ouverte occupe la clé. Inatteignable sous le verrou ci-dessus,
            # mais la garantie doit tenir même si un appelant l'oublie. Rien
            # n'est créé, donc personne n'est renotifié (F18).
            report.suppressed.append(self._suppressed(evaluation, key, SuppressionReason.DUPLICATE))
            return

        enqueued = await self._repository.enqueue_notifications(
            connection, alert_id, evaluation.notify, tms_id=snapshot.tms_id
        )
        report.created.append(
            AlertCreated(
                alert_id=alert_id,
                rule_id=evaluation.rule_id,
                severity=evaluation.severity,
                deduplication_key=key,
                notifications_enqueued=enqueued,
            )
        )

    @staticmethod
    def _suppressed(
        evaluation: RuleEvaluation,
        key: str,
        reason: SuppressionReason,
    ) -> AlertSuppressed:
        assert evaluation.severity is not None  # garanti par l'appelant
        return AlertSuppressed(
            rule_id=evaluation.rule_id,
            severity=evaluation.severity,
            deduplication_key=key,
            reason=reason,
        )

    async def _apply_clearance(
        self,
        connection: AsyncConnection,
        snapshot: MissionSnapshot,
        evaluation: RuleEvaluation,
        report: AlertCycleReport,
    ) -> None:
        """Clôt les alertes d'état dont la règle n'est plus vérifiée (F13).

        Deux garde-fous contre un faux « résolu » (F14) :
          - rien n'est clos sur un instantané partiel : un appel TMS en échec
            ressemble, vu du moteur, à une condition disparue ;
          - les alertes d'événement sont exclues : un incident signalé par le
            chauffeur ne cesse pas d'avoir eu lieu parce que la règle ne
            déclenche plus au cycle suivant.
        """
        if not evaluation.auto_resolve or not snapshot.snapshot_complete:
            return

        open_alerts = await self._repository.open_alerts(
            connection,
            snapshot.tms_id,
            snapshot.mission_id,
            rule_id=evaluation.rule_id,
            state_only=True,
        )
        closable = [alert for alert in open_alerts if alert.auto_resolve]
        await self._close(
            connection,
            closable,
            report,
            resolved_at=evaluation.evaluated_at,
            reason=ResolutionReason.CONDITION_CLEARED,
        )

    async def _resolve_mission_ended(
        self,
        connection: AsyncConnection,
        snapshot: MissionSnapshot,
        report: AlertCycleReport,
    ) -> None:
        """Clôt toutes les alertes d'une mission qui n'est plus en cours (F13).

        Les alertes d'événement sont closes ici, et seulement ici : elles ne
        peuvent pas l'être tant que la mission vit, mais les laisser ouvertes
        après sa fin encombrerait indéfiniment la console des opérateurs.
        """
        open_alerts = await self._repository.open_alerts(connection, snapshot.tms_id, snapshot.mission_id)
        await self._close(
            connection,
            open_alerts,
            report,
            resolved_at=snapshot.observed_at,
            reason=ResolutionReason.MISSION_ENDED,
        )
        await self._audit(connection, report, snapshot.observed_at)

    async def _close(
        self,
        connection: AsyncConnection,
        open_alerts: Sequence[OpenAlert],
        report: AlertCycleReport,
        *,
        resolved_at: datetime,
        reason: ResolutionReason,
    ) -> None:
        if not open_alerts:
            return
        await self._repository.resolve(
            connection,
            [alert.id for alert in open_alerts],
            resolved_at=resolved_at,
            reason=reason,
        )
        report.resolved.extend(
            AlertResolved(alert_id=alert.id, rule_id=alert.rule_id, reason=reason.value)
            for alert in open_alerts
        )

    # --- Audit ---------------------------------------------------------------

    async def _audit(
        self,
        connection: AsyncConnection,
        report: AlertCycleReport,
        tms_now: datetime,
    ) -> None:
        """Journalise les décisions du cycle, y compris les suppressions.

        Les suppressions sont tracées au même titre que les créations : sans
        elles, une alerte attendue et jamais reçue serait inexplicable.
        """
        entries: list[dict[str, object]] = []

        entries.extend(
            {
                "action": "alert_created",
                "tms_id": report.tms_id,
                "mission_id": report.mission_id,
                "alert_id": created.alert_id,
                "rule_id": created.rule_id,
                "detail": {
                    "severity": created.severity.value,
                    "deduplication_key": created.deduplication_key,
                    "notifications_enqueued": created.notifications_enqueued,
                },
                "occurred_at": tms_now,
            }
            for created in report.created
        )
        entries.extend(
            {
                "action": "alert_suppressed",
                "tms_id": report.tms_id,
                "mission_id": report.mission_id,
                "rule_id": suppressed.rule_id,
                "detail": {
                    "reason": suppressed.reason.value,
                    "severity": suppressed.severity.value,
                    "deduplication_key": suppressed.deduplication_key,
                },
                "occurred_at": tms_now,
            }
            for suppressed in report.suppressed
        )
        entries.extend(
            {
                "action": "alert_resolved",
                "tms_id": report.tms_id,
                "mission_id": report.mission_id,
                "alert_id": resolved.alert_id,
                "rule_id": resolved.rule_id,
                "detail": {"reason": resolved.reason},
                "occurred_at": tms_now,
            }
            for resolved in report.resolved
        )
        entries.extend(
            {
                "action": "rule_logged",
                "tms_id": report.tms_id,
                "mission_id": report.mission_id,
                "rule_id": logged.rule_id,
                "detail": {"action": logged.action},
                "occurred_at": tms_now,
            }
            for logged in report.logged
        )

        await self._repository.append_audit(connection, entries)
