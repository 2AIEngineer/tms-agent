"""Rapport de situation à heure fixe (F24).

Un agent de suivi ne pose pas de question à 8 h du matin : il veut trouver
l'état de sa flotte en arrivant. Le mode `report` de l'assistant existe déjà ;
ce module le **déclenche** aux heures que l'exploitation a choisies.

Trois décisions valent d'être expliquées.

**Le rapport ne notifie personne.** Il est produit et conservé comme une
conversation, que l'interface affiche (`GET /assistant/reports`). L'assistant
n'émet ni alerte ni notification (F26), et un rapport poussé par SMS à 8 h
ferait exactement ce que cette règle interdit — avec le risque qu'un rapport
devienne, dans l'esprit des destinataires, un substitut aux alertes.

**Un créneau n'est produit qu'une fois**, même avec plusieurs instances du
service agent. Deux mécanismes se complètent : un verrou consultatif
sérialise les candidats, et le dernier créneau produit est enregistré en base.
Le verrou seul ne suffirait pas — une instance redémarrée à 8 h 02 le
reprendrait aussitôt et produirait un second rapport.

**Un créneau manqué n'est pas rattrapé indéfiniment.** Un service arrêté toute
la nuit ne doit pas produire au démarrage les quatre rapports qu'il a ratés :
seul le créneau le plus récent, et s'il a moins de `catch_up_minutes`, est
produit. Un rapport de la veille n'intéresse personne.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from services.monitoring.state import MonitoringStateRepository
from shared.mapped_dto import AssistantMode, AssistantRequest
from shared.utils.db import Database, advisory_xact_lock

from .service import AssistantService

logger = logging.getLogger(__name__)

# Clé de l'état en base, dans `monitoring_state`.
REPORT_STATE_KEY = "assistant_report"


@dataclass(frozen=True, slots=True)
class ReportSchedule:
    """Les heures auxquelles un rapport est attendu.

    Les heures sont exprimées dans le fuseau de l'exploitation, et non en UTC :
    « 8 h » veut dire 8 h pour l'agent de suivi, y compris après un changement
    d'heure.
    """

    times: tuple[time, ...]
    timezone: str = "UTC"
    # Au-delà, un créneau manqué est abandonné : un rapport de la veille
    # n'intéresse personne.
    catch_up_minutes: int = 30

    @classmethod
    def parse(
        cls,
        times: tuple[str, ...],
        *,
        timezone: str = "UTC",
        catch_up_minutes: int = 30,
    ) -> "ReportSchedule":
        parsed: list[time] = []
        for raw in times:
            hours, _, minutes = raw.strip().partition(":")
            parsed.append(time(hour=int(hours), minute=int(minutes or 0)))
        return cls(
            times=tuple(sorted(parsed)),
            timezone=timezone,
            catch_up_minutes=catch_up_minutes,
        )

    def due_slot(self, now: datetime) -> str | None:
        """Créneau à produire maintenant, ou `None`.

        Rend le créneau **le plus récent** déjà passé et encore frais. Les plus
        anciens sont perdus volontairement : c'est ce qui évite une rafale de
        rapports au démarrage d'un service arrêté depuis la veille.
        """
        if not self.times:
            return None
        local = now.astimezone(ZoneInfo(self.timezone))
        horizon = timedelta(minutes=self.catch_up_minutes)
        candidates = [
            moment
            for moment in (
                local.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0) - offset
                for offset in (timedelta(0), timedelta(days=1))
                for slot in self.times
            )
            if timedelta(0) <= local - moment <= horizon
        ]
        if not candidates:
            return None
        return max(candidates).strftime("%Y-%m-%dT%H:%M")


class ReportScheduler:
    """Produit les rapports de situation aux heures prévues."""

    def __init__(
        self,
        assistant: AssistantService,
        database: Database,
        schedule: ReportSchedule,
        *,
        operator_id: str = "scheduled_report",
        tms_ids: tuple[str, ...] = (),
        prompt: str = "Fais le point sur la situation des missions en cours.",
    ) -> None:
        self._assistant = assistant
        self._database = database
        self._schedule = schedule
        self._operator_id = operator_id
        self._tms_ids = tms_ids
        self._prompt = prompt

    async def run_once(self, now: datetime | None = None) -> str | None:
        """Produit le rapport du créneau courant, s'il y en a un à produire.

        Rend l'identifiant du créneau produit, ou `None` : c'est ce que les
        tests et les journaux ont besoin de distinguer.
        """
        slot = self._schedule.due_slot(now or datetime.now(UTC))
        if slot is None:
            return None
        if not await self._claim(slot):
            return None

        logger.info("Rapport de situation du créneau %s", slot)
        answer = await self._assistant.respond(
            AssistantRequest(
                operator_id=self._operator_id,
                message=self._prompt,
                mode=AssistantMode.REPORT,
                tms_ids=self._tms_ids,
                # Un rapport ne décide de rien : ni clôture, ni appel. Le droit
                # d'agir s'accorde demande par demande, et personne n'est
                # présent à 8 h pour l'accorder.
                allow_actions=False,
            )
        )
        if answer.unavailable:
            # Le créneau est déjà marqué consommé : le modèle indisponible à
            # 8 h ne doit pas faire boucler la production toutes les minutes
            # jusqu'à 8 h 30. L'interface lira que l'assistant était absent.
            logger.warning("Rapport %s produit sans modèle : assistant indisponible", slot)
        return slot

    async def _claim(self, slot: str) -> bool:
        """Réserve le créneau, une seule instance l'obtient.

        Verrou consultatif **et** état en base : le verrou sérialise les
        candidats simultanés, l'état empêche une instance redémarrée de
        reprendre un créneau déjà produit.
        """
        state = MonitoringStateRepository()
        async with self._database.transaction() as connection:
            await advisory_xact_lock(connection, f"{REPORT_STATE_KEY}:{self._operator_id}")
            last = await state.last_cycle(connection, key=self._state_key)
            if last is not None and last.detail.get("last_slot") == slot:
                return False
            await state.record_cycle(
                connection,
                {"last_slot": slot, "operator_id": self._operator_id},
                key=self._state_key,
            )
        return True

    @property
    def _state_key(self) -> str:
        return f"{REPORT_STATE_KEY}:{self._operator_id}"
