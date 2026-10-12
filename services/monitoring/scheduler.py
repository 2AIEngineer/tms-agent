"""Boucle de surveillance : le cœur déterministe du produit.

Un cycle, répété toutes les `poll_interval_s` secondes (F1) :

    heure TMS + instantanés (1 appel MCP)   F2, F3
        -> règles d'état par mission         F6
        -> règles d'événement, une fois      F7
        -> alertes : doublon, cooldown       F11
        -> notifications mises en file       F15
        -> battement de cœur + audit         F27

Trois propriétés gouvernent ce module :

  - **rien n'arrête un cycle.** Une mission illisible, un instantané partiel,
    une règle invalide sont signalés dans le bilan ; le cycle continue sur le
    reste (F4). Seule une panne du serveur MCP interrompt le cycle entier,
    puisqu'il n'y a alors plus rien à lire.
  - **l'heure de référence est celle du TMS**, lue une fois par cycle et
    partagée par toutes les missions : deux missions d'un même cycle doivent
    être jugées sur le même instant (F3).
  - **le LLM n'intervient pas.** La détection et la notification continuent
    sans lui ; seule l'analyse est sautée.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from shared.mapped_dto import MissionSnapshot, ToolError, ToolErrorCode
from shared.utils.db import Database

from .alerts.models import AlertCycleReport
from .alerts.service import AlertService
from .integrations.mcp_client import McpCallFailed, McpClient
from .rules.engine import evaluate_event_rules, evaluate_state_rules
from .rules.loader import RuleRepository, RuleSet
from .state import MonitoringStateRepository, cycle_key

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleReport:
    """Bilan d'un cycle, destiné aux journaux et au tableau de bord."""

    tms_id: str = ""
    # Une heure par TMS récolté.
    tms_time: dict[str, datetime] = field(default_factory=dict)
    # Missions surveillées par TMS, pour qu'un client muet se repère dans le bilan.
    missions_by_tms: dict[str, int] = field(default_factory=dict)
    missions: int = 0
    partial_snapshots: int = 0
    unreadable_missions: dict[str, ToolError] = field(default_factory=dict)
    events_processed: int = 0
    alerts_created: int = 0
    alerts_suppressed: int = 0
    alerts_resolved: int = 0
    rules_logged: int = 0
    active_rules: int = 0
    rule_errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    failed: bool = False
    failure_reason: str | None = None
    # Alertes confiées à l'enquêteur, les plus graves d'abord (F19). Le service
    # agent les reprend depuis la table des alertes : le monitoring ne connaît
    # pas son domaine, il se contente de les nommer dans son bilan.
    investigable: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.failed:
            return f"CYCLE {self.tms_id} EN ÉCHEC — {self.failure_reason}"
        ventilation = (
            " [" + " ".join(f"{tms}={count}" for tms, count in sorted(self.missions_by_tms.items())) + "]"
            if len(self.missions_by_tms) > 1
            else ""
        )
        return (
            f"tms={self.tms_id} missions={self.missions}{ventilation} "
            f"partielles={self.partial_snapshots} "
            f"illisibles={len(self.unreadable_missions)} événements={self.events_processed} "
            f"alertes+{self.alerts_created} supprimées={self.alerts_suppressed} "
            f"closes={self.alerts_resolved} règles={self.active_rules} "
            f"durée={self.duration_s:.2f}s"
        )

    def as_detail(self) -> dict[str, object]:
        """Forme enregistrée dans l'état de surveillance et le journal d'audit."""
        return {
            "tms_id": self.tms_id,
            "tms_time": {tms: moment.isoformat() for tms, moment in self.tms_time.items()},
            "missions": self.missions,
            "missions_by_tms": self.missions_by_tms,
            "partial_snapshots": self.partial_snapshots,
            "unreadable_missions": sorted(self.unreadable_missions),
            "events_processed": self.events_processed,
            "alerts_created": self.alerts_created,
            "alerts_suppressed": self.alerts_suppressed,
            "alerts_resolved": self.alerts_resolved,
            "active_rules": self.active_rules,
            "rule_errors": self.rule_errors,
            "duration_s": round(self.duration_s, 3),
            "failed": self.failed,
            "failure_reason": self.failure_reason,
        }


class MonitoringCycle:
    """Exécute un cycle de surveillance de bout en bout."""

    def __init__(
        self,
        database: Database,
        mcp: McpClient,
        rules: RuleRepository,
        alerts: AlertService,
        *,
        state: MonitoringStateRepository | None = None,
    ) -> None:
        self._database = database
        self._mcp = mcp
        self._rules = rules
        self._alerts = alerts
        self._state = state or MonitoringStateRepository()

    async def run_once(self, tms_id: str) -> CycleReport:
        """Exécute un cycle pour **un** TMS.

        L'unité de travail est le TMS : un TMS en panne recule seul, un TMS
        silencieux est détectable séparément, et plusieurs TMS peuvent être
        surveillés en parallèle sans que l'un retarde l'autre.
        """
        started = time.monotonic()
        report = CycleReport(tms_id=tms_id)

        # Rechargement à chaud avant toute évaluation : une modification de
        # seuil déposée entre deux cycles doit s'appliquer dès celui-ci (F8).
        ruleset = self._rules.reload_if_changed()
        report.active_rules = len(ruleset.state_rules) + len(ruleset.event_rules)
        report.rule_errors = [str(error) for error in ruleset.errors]

        try:
            harvest = await self._mcp.collect_cycle(tms_id)
        except McpCallFailed as failure:
            # Serveur MCP ou TMS injoignable : il n'y a rien à lire, donc rien
            # à décider. Le cycle est signalé en échec et l'appelant attend
            # avec un backoff exponentiel (F5).
            report.failed = True
            report.failure_reason = failure.error.message
            report.duration_s = time.monotonic() - started
            logger.error("Cycle en échec : %s", failure.error.message)
            await self._record(report)
            return report

        report.tms_time = harvest.tms_time
        report.missions = len(harvest.snapshots)
        report.partial_snapshots = len(harvest.partial)
        report.unreadable_missions = harvest.failures
        for snapshot in harvest.snapshots:
            report.missions_by_tms[snapshot.tms_id] = report.missions_by_tms.get(snapshot.tms_id, 0) + 1

        for snapshot in harvest.snapshots:
            await self._process_mission(snapshot, ruleset, report)

        report.duration_s = time.monotonic() - started
        await self._record(report)

        if report.duration_s > 0:
            logger.info("Cycle terminé — %s", report.summary())
        for mission_id, error in harvest.failures.items():
            logger.warning("Mission %s illisible : %s", mission_id, error.message)
        return report

    async def _process_mission(
        self,
        snapshot: MissionSnapshot,
        ruleset: RuleSet,
        report: CycleReport,
    ) -> None:
        """Évalue une mission et applique les décisions d'alerte.

        Les erreurs sont absorbées par mission : un défaut sur une mission ne
        doit pas priver les autres de surveillance pendant tout un cycle (F4).
        """
        try:
            state_report = await self._alerts.process_state_evaluations(
                snapshot, evaluate_state_rules(ruleset.rules, snapshot)
            )
            self._merge(state_report, report)

            # Les règles d'événement ne sont évaluées qu'une fois par événement
            # nouveau : le TMS renvoie toute la chronologie à chaque appel (F7).
            new_events = await self._alerts.filter_new_events(snapshot.tms_id, snapshot.events)
            for event in new_events:
                event_report = await self._alerts.process_event_evaluations(
                    snapshot, event, evaluate_event_rules(ruleset.rules, snapshot, event)
                )
                self._merge(event_report, report)
            report.events_processed += len(new_events)
        except Exception as exception:
            logger.exception("Mission %s non traitée", snapshot.mission_id)
            report.unreadable_missions[f"{snapshot.tms_id}:{snapshot.mission_id}"] = ToolError.of(
                ToolErrorCode.PARTIAL_DATA, f"échec de traitement : {exception}"
            )

    @staticmethod
    def _merge(mission_report: AlertCycleReport, report: CycleReport) -> None:
        report.alerts_created += len(mission_report.created)
        report.alerts_suppressed += len(mission_report.suppressed)
        report.alerts_resolved += len(mission_report.resolved)
        report.rules_logged += len(mission_report.logged)
        report.investigable.extend(str(created.alert_id) for created in mission_report.investigable)

    async def _record(self, report: CycleReport) -> None:
        """Écrit le battement de cœur, y compris pour un cycle en échec.

        Un cycle en échec est enregistré lui aussi : c'est la différence entre
        « le worker tourne mais n'arrive pas à lire le TMS » et « le worker ne
        tourne plus », deux pannes qui n'appellent pas la même intervention.
        """
        async with self._database.transaction() as connection:
            # Un battement par TMS : un client silencieux doit être détectable
            # même si les autres continuent de tourner. Un battement global le
            # masquerait dès qu'un seul TMS répond encore.
            await self._state.record_cycle(connection, report.as_detail(), key=cycle_key(report.tms_id))
