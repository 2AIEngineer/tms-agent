"""Point d'entrée du worker de surveillance.

**L'unité de travail est le TMS, pas le cycle.** Le worker lance une boucle de
surveillance indépendante par TMS, plus une boucle d'expédition des
notifications. Toutes tournent en parallèle dans le même processus : elles sont
bornées par des attentes réseau, pas par du calcul.

Pourquoi une boucle par TMS plutôt qu'un cycle unique qui les parcourt :

  - **en série, un cycle ne tient pas.** Avec cent missions par TMS et six
    lectures simultanées, un TMS coûte quelques secondes ; vingt TMS les
    additionneraient et le cycle déborderait largement l'intervalle de dix
    secondes. Une alerte critique sur le vingtième client arriverait en retard
    par la seule faute des dix-neuf premiers ;
  - **chaque TMS recule seul.** Un TMS en panne applique son propre backoff
    sans ralentir les autres (F5) ;
  - **chaque TMS a son battement de cœur.** Un client silencieux est détectable
    même quand les autres tournent — un battement global le masquerait ;
  - **le verrou devient le mécanisme de répartition** entre instances, sans
    coordinateur (voir ci-dessous).

**Répartition entre instances.** Chaque TMS est protégé par un verrou
consultatif PostgreSQL qui lui est propre. Un worker tente de prendre jusqu'à
`max_tms_per_worker` verrous et surveille ce qu'il a obtenu. Lancer une seconde
instance suffit alors à répartir la charge : elle prend les TMS que la première
n'a pas pu tenir. Le plafond est ce qui transforme le verrou en répartiteur —
sans lui, la première instance prendrait tout.

Les verrous étant tenus par la connexion, un worker qui meurt les libère, et
une autre instance reprend ses TMS au balayage suivant. Aucun coordinateur,
aucun registre d'affectation à maintenir.

Un seul conteneur par TMS serait du gaspillage pour vingt clients — vingt
applications à déployer et à payer à l'arrêt — et reste possible malgré tout :
il suffit de lancer le worker avec `MONITORING_TMS_ALLOWLIST` réduit à un TMS.

Lancement :
    uv run python -m services.monitoring.main
"""

import asyncio
import contextlib
import logging
import signal
import zlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from shared.channels import console_registry
from shared.utils.db import Database, advisory_unlock, try_advisory_lock

from .alerts.service import AlertService
from .config import MonitoringSettings, get_settings
from .integrations.mcp_client import ConnectedTms, McpCallFailed, McpClient
from .notifications import NotificationService
from .rules.loader import RuleRepository
from .scheduler import MonitoringCycle

logger = logging.getLogger(__name__)


def tms_lock_key(base_key: int, tms_id: str) -> int:
    """Identifiant du verrou consultatif d'un TMS.

    Dérivé du nom par CRC32 : stable d'une instance et d'un redémarrage à
    l'autre, ce qui est la seule propriété nécessaire. Une collision entre deux
    noms ferait surveiller un TMS par personne — raison pour laquelle le
    worker journalise la clé obtenue pour chaque TMS.
    """
    return (base_key + zlib.crc32(tms_id.encode())) % (2**31)


@dataclass(slots=True)
class OwnedTms:
    """Un TMS que ce worker a réussi à verrouiller, donc qu'il surveille."""

    tms: ConnectedTms
    lock_key: int
    connection: AsyncConnection


class MonitoringWorker:
    def __init__(self, settings: MonitoringSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        """Demande l'arrêt. Les cycles en cours se terminent avant de rendre la main."""
        if not self._stopping.is_set():
            logger.info("Arrêt demandé — fin des cycles en cours")
            self._stopping.set()

    # --- Démarrage ------------------------------------------------------------

    async def run(self) -> None:
        settings = self._settings
        database = Database(settings.database_url, pool_size=settings.database_pool_size)

        try:
            async with McpClient(settings.mcp_server_url) as mcp:
                available = await self._discover(mcp)
                if not available:
                    return

                async with self._claim(database, available) as owned:
                    if not owned:
                        # Tous les TMS sont déjà surveillés par d'autres
                        # instances : sortir est la bonne réponse, et non
                        # tourner à vide en consommant une connexion.
                        logger.error(
                            "Aucun TMS disponible : les %d TMS sont déjà tenus par "
                            "d'autres instances — arrêt",
                            len(available),
                        )
                        return
                    await self._run_loops(database, mcp, owned)
        finally:
            await database.dispose()
            logger.info("Worker de surveillance arrêté")

    async def _discover(self, mcp: McpClient) -> list[ConnectedTms]:
        """Demande au serveur MCP quels TMS sont raccordés.

        Le worker ne lit pas la configuration des connexions : elle appartient
        au serveur MCP. Une seule source de vérité, et le worker n'a pas besoin
        d'être redéployé quand un TMS est ajouté.
        """
        try:
            connected = await mcp.list_tms()
        except McpCallFailed as failure:
            logger.error("Serveur MCP injoignable au démarrage : %s", failure.error.message)
            return []

        allowlist = self._settings.tms_allowlist
        if allowlist:
            connected = [tms for tms in connected if tms.tms_id in allowlist]

        if not connected:
            logger.error("Aucun TMS à surveiller (raccordés filtrés par l'allowlist ?)")
            return []

        for tms in connected:
            if tms.degraded:
                # Journalisé au démarrage, et non découvert plus tard devant une
                # absence d'alerte : sur ce TMS, certaines règles ne pourront
                # jamais déclencher.
                logger.warning(
                    "TMS %s dégradé : règles inopérantes — %s",
                    tms.tms_id,
                    ", ".join(tms.inoperative_rules) or "aucune nommée",
                )
        return connected

    @asynccontextmanager
    async def _claim(
        self,
        database: Database,
        available: list[ConnectedTms],
    ) -> AsyncGenerator[list[OwnedTms]]:
        """Prend un verrou par TMS, dans la limite du plafond de ce worker.

        Chaque verrou est tenu par **une connexion dédiée**, conservée ouverte
        tant que le TMS est surveillé. Le prendre sur une connexion du pool ne
        garantirait rien : le pool la rendrait, et le verrou tomberait au
        premier cycle.
        """
        settings = self._settings
        owned: list[OwnedTms] = []
        try:
            for tms in available:
                if len(owned) >= settings.max_tms_per_worker:
                    logger.info(
                        "Plafond atteint (%d TMS) : %s laissé à une autre instance",
                        settings.max_tms_per_worker,
                        tms.tms_id,
                    )
                    continue

                key = tms_lock_key(settings.singleton_lock_key, tms.tms_id)
                connection = await database.engine.connect().start()
                if await try_advisory_lock(connection, key):
                    owned.append(OwnedTms(tms=tms, lock_key=key, connection=connection))
                    logger.info("TMS %s pris en charge (verrou %d)", tms.tms_id, key)
                else:
                    await connection.close()
                    logger.info("TMS %s déjà surveillé par une autre instance", tms.tms_id)
            yield owned
        finally:
            for item in owned:
                with contextlib.suppress(Exception):
                    await advisory_unlock(item.connection, item.lock_key)
                    await item.connection.close()

    # --- Boucles --------------------------------------------------------------

    async def _run_loops(
        self,
        database: Database,
        mcp: McpClient,
        owned: list[OwnedTms],
    ) -> None:
        settings = self._settings
        alerts = AlertService(database)
        # Un seul jeu de règles, partagé : elles ne dépendent pas du TMS, et le
        # recharger par TMS relirait les mêmes fichiers autant de fois.
        rules = RuleRepository(settings.rules_directory)
        cycle = MonitoringCycle(database, mcp, rules, alerts)

        notifier = NotificationService(
            database,
            mcp,
            console_registry(),
            batch_size=settings.notification_batch_size,
            lease_s=settings.notification_lease_s,
            max_attempts=settings.notification_max_attempts,
            retry_backoff_s=settings.notification_retry_backoff_s,
            retry_backoff_max_s=settings.notification_retry_backoff_max_s,
        )

        logger.info(
            "Worker démarré — %d TMS surveillé(s) : %s | cycle %ds | MCP %s",
            len(owned),
            ", ".join(item.tms.tms_id for item in owned),
            settings.poll_interval_s,
            settings.mcp_server_url,
        )

        await asyncio.gather(
            *(self._surveillance_loop(cycle, item.tms.tms_id) for item in owned),
            self._dispatch_loop(notifier),
        )

    async def _surveillance_loop(self, cycle: MonitoringCycle, tms_id: str) -> None:
        """Boucle de détection d'un TMS, avec son propre backoff (F5).

        Le backoff est **par TMS** : un client en panne ne doit pas espacer la
        surveillance des autres.
        """
        settings = self._settings
        consecutive_failures = 0

        while not self._stopping.is_set():
            try:
                report = await cycle.run_once(tms_id)
            except Exception:
                logger.exception("Cycle %s interrompu par une erreur inattendue", tms_id)
                consecutive_failures += 1
                await self._sleep_or_stop(self._backoff(consecutive_failures))
                continue

            if report.failed:
                consecutive_failures += 1
                delay = self._backoff(consecutive_failures)
                logger.warning(
                    "Cycle %s en échec (%d d'affilée) — nouvelle tentative dans %ds",
                    tms_id,
                    consecutive_failures,
                    delay,
                )
            else:
                if consecutive_failures:
                    logger.info(
                        "Surveillance de %s rétablie après %d échec(s)",
                        tms_id,
                        consecutive_failures,
                    )
                consecutive_failures = 0
                # Le délai est décompté *après* le cycle : un cycle lent
                # n'accumule pas de retard, il repart simplement plus tard.
                delay = max(0.0, settings.poll_interval_s - report.duration_s)

            await self._sleep_or_stop(delay)

    def _backoff(self, consecutive_failures: int) -> float:
        """Attente croissante, plafonnée : marteler un TMS en panne n'y change rien."""
        delay: float = min(
            self._settings.poll_interval_s * (2**consecutive_failures),
            self._settings.cycle_backoff_max_s,
        )
        return delay

    async def _dispatch_loop(self, notifier: NotificationService) -> None:
        """Boucle d'expédition de la file de notifications.

        Une seule boucle pour tous les TMS : la file est commune, et chaque
        demande porte sa provenance. Rythme indépendant de la surveillance —
        lorsqu'il reste des demandes, elle enchaîne ; sinon elle se met en
        veille. Une rafale de vingt alertes part ainsi en quelques secondes.
        """
        while not self._stopping.is_set():
            try:
                report = await notifier.dispatch_once()
            except Exception:
                logger.exception("Passage d'expédition en erreur")
                await self._sleep_or_stop(self._settings.poll_interval_s)
                continue

            empty = report.claimed == 0
            await self._sleep_or_stop(self._settings.poll_interval_s if empty else 0.5)

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Attend, mais rend la main immédiatement si l'arrêt est demandé."""
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    worker = MonitoringWorker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # SIGTERM est le signal d'arrêt des conteneurs : l'ignorer ferait tuer le
    # worker au milieu d'un cycle à chaque déploiement.
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, worker.request_stop)

    try:
        loop.run_until_complete(worker.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
