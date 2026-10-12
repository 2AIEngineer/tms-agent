"""Garde-fous sur les appels au TMS.

Le TMS est un système tiers que nous ne contrôlons pas, interrogé par polling
toutes les dix secondes sur des centaines de missions. Le saturer est le risque
principal de cette architecture : ces garde-fous sont le seul endroit qui nous
en empêche.

Le plafond est appliqué **globalement au processus**, et non par mission ni par
appelant : c'est le TMS qui a une limite, pas nous.
"""

import asyncio
import time
from types import TracebackType


class RateLimiter:
    """Limiteur à seau de jetons, lissant le débit vers le TMS.

    Un seau et non une fenêtre fixe : une fenêtre autoriserait 25 requêtes dans
    la même milliseconde puis plus rien pendant une seconde, ce qui ressemble à
    une rafale d'attaque vu du TMS. Le seau étale les appels.
    """

    def __init__(self, rate_per_second: float, *, burst: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("Le débit doit être strictement positif")
        self._rate = rate_per_second
        self._capacity = burst if burst is not None else max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Attend qu'un jeton soit disponible. Rend le temps attendu, en secondes.

        Le temps d'attente est renvoyé pour être mesuré : s'il devient non
        négligeable, c'est que la flotte surveillée dépasse ce que le quota
        autorise, et qu'il faut relever le plafond ou espacer les cycles.
        """
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                missing = 1.0 - self._tokens
                delay = missing / self._rate
            await asyncio.sleep(delay)
            waited += delay

    def _refill(self) -> None:
        current = time.monotonic()
        elapsed = current - self._updated_at
        self._updated_at = current
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens


class TmsGuardrails:
    """Plafonne débit et parallélisme des appels au TMS.

    Les deux limites sont complémentaires : le débit protège le TMS sur la
    durée, le parallélisme empêche d'ouvrir cinquante connexions simultanées
    lors d'une rafale initiale, quand le seau est plein.
    """

    def __init__(self, *, max_requests_per_second: float, max_concurrent: int) -> None:
        self._limiter = RateLimiter(max_requests_per_second)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._total_requests = 0
        self._total_wait_s = 0.0

    def slot(self) -> "_GuardrailSlot":
        """Réserve un droit d'appel, à utiliser comme gestionnaire de contexte."""
        return _GuardrailSlot(self)

    @property
    def stats(self) -> dict[str, float]:
        """Compteurs d'exploitation, remontés dans le bilan de cycle."""
        return {
            "requests": float(self._total_requests),
            "throttled_seconds": round(self._total_wait_s, 3),
        }

    async def _enter(self) -> None:
        await self._semaphore.acquire()
        try:
            self._total_wait_s += await self._limiter.acquire()
            self._total_requests += 1
        except BaseException:
            # Le jeton n'a pas été obtenu : rendre la place pour ne pas fuir une
            # unité de parallélisme à chaque annulation.
            self._semaphore.release()
            raise

    def _exit(self) -> None:
        self._semaphore.release()


class _GuardrailSlot:
    def __init__(self, guardrails: TmsGuardrails) -> None:
        self._guardrails = guardrails

    async def __aenter__(self) -> None:
        await self._guardrails._enter()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._guardrails._exit()
