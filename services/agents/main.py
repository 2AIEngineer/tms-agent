"""Point d'entrée du service agent : consommateur d'enquêtes.

    alertes éligibles sans enquête
        -> réclamation atomique (F19, les plus graves d'abord)
        -> faits déterministes (F21)
        -> boucle d'enquête : modèle + outils MCP
        -> évaluation structurée jointe à l'alerte (F22)

Deux garanties portent ce service, et toutes deux découlent de la même idée :
**la détection ne dépend jamais de l'enquête.**

  - **l'enquête tourne en arrière-plan** (F20). Le cycle de surveillance ne
    l'attend pas : les deux services sont des processus distincts, reliés par
    la seule table des alertes ;
  - **un échec d'enquête ne touche pas l'alerte.** Elle reste ouverte et
    notifiée ; seule l'explication manque. Sans LLM — pas de clé, quota épuisé,
    service éteint — le système continue d'alerter.

Contrairement au worker de surveillance, ce service n'a pas besoin de verrou :
la réclamation d'une enquête est elle-même atomique. Plusieurs instances se
répartissent donc le travail sans configuration, et en ajouter une augmente le
débit d'enquêtes sans rien coordonner.

Lancement :
    uv run python -m services.agents.main
"""

import asyncio
import contextlib
import logging
import signal
import uuid
from datetime import UTC, datetime, timedelta

from services.monitoring.rules.loader import RuleRepository
from shared.channels import ConsoleChannel
from shared.mapped_dto import (
    InvestigationRequest,
    InvestigationStatus,
    MissionSnapshot,
    NotificationChannel,
)
from shared.utils.db import Database

from .assistant.actions import AssistantActions
from .assistant.schedule import ReportScheduler
from .assistant.service import AssistantService
from .assistant.tools import ASSISTANT_READ_TOOLS
from .config import AgentSettings, get_settings
from .integrations.litellm_client import LiteLlmClient
from .integrations.mcp_client import AgentMcpClient
from .investigator.facts import build_observed_facts
from .investigator.loop import Investigator, LocalTool
from .investigator.repository import InvestigationRepository
from .local_tools import AgentHistoryTool, RuleCheckTool
from .operations.driver_call import DriverCallService
from .operations.tool import DriverCallTool

logger = logging.getLogger(__name__)

# Au-delà, une enquête `running` est considérée abandonnée par l'instance qui
# l'avait réclamée. Généreux à dessein : une enquête lente est normale, et la
# reprendre trop tôt la ferait tourner deux fois.
STALE_AFTER = timedelta(minutes=15)

# Délai avant de reprendre une enquête sautée faute de modèle (quota épuisé,
# service éteint). Court à dessein : l'alerte attend son analyse depuis le
# début, et un quota par minute se libère en une minute. Le plafond de
# tentatives empêche la boucle si le modèle reste absent.
RETRY_SKIPPED_AFTER = timedelta(minutes=2)


class AgentWorker:
    def __init__(self, settings: AgentSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()
        self._repository = InvestigationRepository()

    def request_stop(self) -> None:
        if not self._stopping.is_set():
            logger.info("Arrêt demandé — fin des enquêtes en cours")
            self._stopping.set()

    async def run(self) -> None:
        settings = self._settings
        database = Database(settings.database_url)
        llm = LiteLlmClient(
            settings.litellm_base_url,
            settings.litellm_api_key,
            model=settings.model,
            temperature=settings.temperature,
            timeout_s=settings.request_timeout_s,
            max_output_tokens=settings.max_output_tokens,
        )

        try:
            async with AgentMcpClient(settings.mcp_server_url) as mcp:
                investigator = Investigator(
                    llm,
                    mcp,
                    max_tool_rounds=settings.max_tool_rounds,
                    local_tools=self._local_tools(settings, database, mcp),
                    max_input_tokens=settings.max_input_tokens,
                )
                logger.info(
                    "Service agent démarré — modèle %s, %d enquête(s) en parallèle, seuil %s, MCP %s",
                    settings.model,
                    settings.max_concurrent_investigations,
                    settings.investigate_min_severity.value,
                    settings.mcp_server_url,
                )
                if not settings.driver_call_enabled:
                    logger.info("Appel au chauffeur désactivé — l'enquête s'en tiendra aux lectures")

                # Le rapport de situation tourne **à côté** des enquêtes, dans
                # le même processus : les deux workflows partagent le client
                # MCP, l'inférence et la configuration, et un rapport toutes
                # les quelques heures ne justifie pas un service de plus.
                async with contextlib.AsyncExitStack() as stack:
                    assistant = await self._assistant(stack, settings, database, llm)
                    reports = self._report_scheduler(settings, assistant, database)
                    await asyncio.gather(
                        self._consume(database, investigator, mcp),
                        self._produce_reports(reports),
                        # Les actions en attente de validation se périment, et
                        # une exécution peut être perdue par un arrêt brutal :
                        # ce balayage est ce qui empêche l'interface d'afficher
                        # « en attente » sur un geste que plus personne ne tient.
                        self._sweep_actions(assistant),
                    )
        finally:
            await llm.aclose()
            await database.dispose()
            logger.info("Service agent arrêté")

    @staticmethod
    async def _assistant(
        stack: contextlib.AsyncExitStack,
        settings: AgentSettings,
        database: Database,
        llm: LiteLlmClient,
    ) -> AssistantService:
        """Monte l'assistant du service agent.

        Un second client MCP, car l'assistant et l'enquêteur n'ont pas la même
        liste blanche d'outils : l'assistant interroge la flotte, l'enquêteur
        une mission désignée.

        Cette instance sert au rapport planifié et au balayage des actions en
        attente. Les conversations des opérateurs, elles, passent par l'API
        d'exploitation, qui monte la sienne — les deux ne partagent que la base,
        qui porte tout l'état.
        """
        client = await stack.enter_async_context(
            AgentMcpClient(settings.mcp_server_url, allowed=ASSISTANT_READ_TOOLS)
        )
        return AssistantService(
            llm,
            client,
            AssistantActions(database, client),
            database=database,
            action_ttl=settings.assistant_action_ttl,
            max_input_tokens=settings.max_input_tokens,
        )

    @staticmethod
    def _report_scheduler(
        settings: AgentSettings,
        assistant: AssistantService,
        database: Database,
    ) -> "ReportScheduler | None":
        """Calendrier des rapports, si la fonction est activée."""
        if not settings.report_enabled:
            return None
        schedule = settings.report_schedule()
        if not schedule.times:
            logger.warning("Rapports activés sans heure configurée : aucun ne sera produit")
            return None
        logger.info(
            "Rapport de situation à %s (%s)",
            ", ".join(moment.strftime("%H:%M") for moment in schedule.times),
            schedule.timezone,
        )
        return ReportScheduler(
            assistant,
            database,
            schedule,
            operator_id=settings.report_operator_id,
            tms_ids=settings.report_tms_ids,
        )

    async def _sweep_actions(self, assistant: AssistantService) -> None:
        """Solde périodiquement les actions que le temps a tranchées."""
        while not self._stopping.is_set():
            try:
                await assistant.sweep_actions()
            except Exception:
                logger.exception("Balayage des actions en attente en échec")
            await self._sleep_or_stop(60)

    async def _produce_reports(self, scheduler: "ReportScheduler | None") -> None:
        """Vérifie le calendrier chaque minute.

        Une minute suffit : un rapport attendu à 8 h et produit à 8 h 00 min 40
        est à l'heure, et sonder plus souvent n'apporterait rien.
        """
        if scheduler is None:
            return
        while not self._stopping.is_set():
            try:
                await scheduler.run_once()
            except Exception:
                # Un rapport en échec ne doit pas emporter les enquêtes, qui
                # tournent dans la même tâche groupée.
                logger.exception("Rapport de situation en échec")
            await self._sleep_or_stop(60)

    @staticmethod
    def _local_tools(
        settings: AgentSettings,
        database: Database,
        mcp: AgentMcpClient,
    ) -> list[LocalTool]:
        """Outils servis par l'agent lui-même, et non par MCP.

        Deux lectures de notre propre domaine — notre historique sur la mission
        et la réévaluation des règles (F21) — et une action, l'appel au
        chauffeur, qui se coupe : `DRIVER_CALL_ENABLED=false` retire l'outil de
        la liste plutôt que de refuser les appels. Un outil absent ne tente pas
        le modèle et n'occupe pas sa fenêtre de contexte.
        """
        tools: list[LocalTool] = [
            AgentHistoryTool(database),
            RuleCheckTool(RuleRepository(settings.rules_directory), mcp),
        ]
        if settings.driver_call_enabled:
            # En développement, l'appel est servi par le canal console : la
            # trace et les garde-fous sont réels, seul le fournisseur de
            # téléphonie manque. Le brancher ne touchera que cette ligne.
            channel = ConsoleChannel(NotificationChannel.CALL)
            service = DriverCallService(channel, guardrails=settings.guardrails())
            tools.append(DriverCallTool(service, database, mcp))
        return tools

    async def _consume(
        self,
        database: Database,
        investigator: Investigator,
        mcp: AgentMcpClient,
    ) -> None:
        settings = self._settings
        # F20 : deux enquêtes en parallèle. Le plafond protège surtout le quota
        # du fournisseur d'inférence, qui s'épuise bien avant notre processus.
        semaphore = asyncio.Semaphore(settings.max_concurrent_investigations)

        while not self._stopping.is_set():
            try:
                claimed = await self._claim(database)
            except Exception:
                logger.exception("Réclamation d'enquêtes en échec")
                await self._sleep_or_stop(settings.poll_interval_s)
                continue

            if not claimed:
                await self._sleep_or_stop(settings.poll_interval_s)
                continue

            async def one(investigation_id: uuid.UUID) -> None:
                async with semaphore:
                    await self._investigate(database, investigator, mcp, investigation_id)

            await asyncio.gather(*(one(item) for item in claimed))

    async def _claim(self, database: Database) -> list[uuid.UUID]:
        """Réclame un lot d'enquêtes, reprises comprises."""
        settings = self._settings
        async with database.transaction() as connection:
            # Les enquêtes abandonnées passent avant les nouvelles : une alerte
            # dont l'enquête a été interrompue attend déjà depuis un moment.
            stale = await self._repository.reclaim_stale(
                connection,
                older_than=datetime.now(UTC) - STALE_AFTER,
                max_attempts=settings.max_attempts,
            )
            # Les enquêtes sautées faute de modèle sont reprises dès que le
            # quota se libère. Le délai est court : l'alerte, elle, attend son
            # analyse depuis le début.
            skipped = await self._repository.reclaim_skipped(
                connection,
                older_than=datetime.now(UTC) - RETRY_SKIPPED_AFTER,
                max_attempts=settings.max_attempts,
            )
            reclaimed = [*stale, *skipped]
            fresh = await self._repository.claim_batch(
                connection,
                min_severity=settings.investigate_min_severity,
                limit=max(1, settings.investigations_per_batch - len(reclaimed)),
            )
        if stale:
            logger.info("%d enquête(s) abandonnée(s) reprise(s)", len(stale))
        if skipped:
            logger.info("%d enquête(s) sautée(s) reprise(s) — modèle de nouveau disponible", len(skipped))
        return [*reclaimed, *fresh]

    async def _investigate(
        self,
        database: Database,
        investigator: Investigator,
        mcp: AgentMcpClient,
        investigation_id: uuid.UUID,
    ) -> None:
        """Conduit une enquête et enregistre son issue.

        Toute exception est rattrapée : une enquête qui casse ne doit pas
        emporter les autres, ni le service.
        """
        async with database.connection() as connection:
            context = await self._repository.load(connection, investigation_id)
        if context is None:
            logger.warning("Enquête %s introuvable après réclamation", investigation_id)
            return

        alert = context.alert
        try:
            # Le snapshot est relu par le serveur MCP : la mission a pu évoluer
            # depuis la détection, et l'enquête doit porter sur l'état courant.
            snapshot = await self._load_snapshot(mcp, alert.tms_id, alert.mission_id)
            facts = build_observed_facts(alert, snapshot)

            async with database.transaction() as connection:
                await self._repository.record_facts(connection, investigation_id, facts)

            request = InvestigationRequest(
                investigation_id=investigation_id,
                alert_id=alert.id,
                tms_id=alert.tms_id,
                mission_id=alert.mission_id,
                alert=alert,
                observed_facts=facts,
                notifications_sent=context.notifications_sent,
                requested_at=datetime.now(UTC),
            )
            outcome = await investigator.investigate(request)
        except Exception as exception:
            logger.exception("Enquête %s interrompue", investigation_id)
            async with database.transaction() as connection:
                await self._repository.fail(connection, investigation_id, str(exception))
            return

        async with database.transaction() as connection:
            if outcome.result is not None:
                await self._repository.complete(
                    connection,
                    investigation_id,
                    outcome.result,
                    model=outcome.model,
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                    duration_ms=outcome.duration_ms,
                )
                logger.info(
                    "Enquête %s aboutie — risque %s, confiance %.2f, %d jeton(s), %dms",
                    investigation_id,
                    outcome.result.risk.value,
                    outcome.result.confidence,
                    outcome.usage.input_tokens + outcome.usage.output_tokens,
                    outcome.duration_ms,
                )
            else:
                await self._repository.fail(
                    connection,
                    investigation_id,
                    outcome.error or "échec sans motif",
                    # « Sauté » dit que le LLM n'était pas là ; « en échec » dit
                    # qu'un défaut nous appartient. Les confondre rendrait les
                    # deux illisibles dans le tableau de bord.
                    status=(InvestigationStatus.SKIPPED if outcome.skipped else InvestigationStatus.FAILED),
                )

    @staticmethod
    async def _load_snapshot(
        mcp: AgentMcpClient,
        tms_id: str,
        mission_id: str,
    ) -> MissionSnapshot | None:
        """Relit l'instantané de la mission, sans faire échouer l'enquête.

        Un instantané absent est un cas prévu : la mission a pu se terminer
        entre la détection et l'enquête. Les faits le signalent explicitement
        plutôt que de laisser le modèle conclure de ce silence.
        """
        result = await mcp.call("get_mission_snapshot", {"tms_id": tms_id, "mission_id": mission_id})
        raw = result.payload.get("snapshot") if result.ok else None
        if not isinstance(raw, dict):
            return None
        try:
            return MissionSnapshot.model_validate(raw)
        except Exception:
            logger.warning("Instantané de %s non conforme : enquête sur les seuls faits", mission_id)
            return None

    async def _sleep_or_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    worker = AgentWorker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, worker.request_stop)

    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
