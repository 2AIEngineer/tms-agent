"""Tests des garde-fous vers le TMS.

Le TMS est un système tiers interrogé toutes les dix secondes sur des centaines
de missions : le saturer est le risque principal de cette architecture.
"""

import asyncio
import time

import pytest

from services.mcp_server.guardrails import RateLimiter, TmsGuardrails


class TestRateLimiter:
    async def test_premieres_requetes_immediates(self):
        """Le seau est plein au démarrage : un cycle ne doit pas être freiné d'emblée."""
        limiter = RateLimiter(10.0)
        waited = [await limiter.acquire() for _ in range(10)]
        assert all(delay == 0.0 for delay in waited)

    async def test_requete_excedentaire_attend(self):
        limiter = RateLimiter(20.0, burst=2)
        await limiter.acquire()
        await limiter.acquire()

        started = time.monotonic()
        waited = await limiter.acquire()
        elapsed = time.monotonic() - started

        assert waited > 0
        assert elapsed >= 0.04  # ~1/20 s pour reconstituer un jeton

    async def test_debit_respecte_sous_charge(self):
        """La propriété qui compte : le TMS ne voit jamais plus que le débit permis."""
        limiter = RateLimiter(50.0, burst=1)
        started = time.monotonic()
        await asyncio.gather(*(limiter.acquire() for _ in range(10)))
        elapsed = time.monotonic() - started

        # 10 jetons à 50/s avec un seau de 1 : au moins 9 recharges.
        assert elapsed >= 9 / 50 * 0.8

    async def test_debit_invalide_refuse(self):
        with pytest.raises(ValueError):
            RateLimiter(0)


class TestTmsGuardrails:
    async def test_parallelisme_plafonne(self):
        """Empêche d'ouvrir cinquante connexions au TMS quand le seau est plein."""
        guardrails = TmsGuardrails(max_requests_per_second=1000.0, max_concurrent=3)
        active = 0
        peak = 0

        async def call() -> None:
            nonlocal active, peak
            async with guardrails.slot():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(call() for _ in range(20)))
        assert peak <= 3

    async def test_compteurs_dexploitation(self):
        """Le temps d'attente est mesuré : s'il grimpe, le quota est trop bas."""
        guardrails = TmsGuardrails(max_requests_per_second=1000.0, max_concurrent=5)
        for _ in range(4):
            async with guardrails.slot():
                pass

        assert guardrails.stats["requests"] == 4
        assert guardrails.stats["throttled_seconds"] >= 0

    async def test_place_rendue_meme_sur_erreur(self):
        """Une exception ne doit pas faire fuir une unité de parallélisme."""
        guardrails = TmsGuardrails(max_requests_per_second=1000.0, max_concurrent=1)

        with pytest.raises(RuntimeError):
            async with guardrails.slot():
                raise RuntimeError("panne au milieu d'un appel")

        # Si la place n'avait pas été rendue, cet appel resterait bloqué.
        async with asyncio.timeout(1):
            async with guardrails.slot():
                pass

    async def test_place_rendue_sur_annulation(self):
        guardrails = TmsGuardrails(max_requests_per_second=1000.0, max_concurrent=1)

        async def cancelled() -> None:
            async with guardrails.slot():
                await asyncio.sleep(10)

        task = asyncio.create_task(cancelled())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        async with asyncio.timeout(1):
            async with guardrails.slot():
                pass
