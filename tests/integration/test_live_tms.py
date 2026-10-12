"""Tests contre le vrai TMS simulé (`x_tms`), s'il est en écoute.

Les tests précédents utilisent un double fidèle du TMS. Celui-ci vérifie la
seule chose qu'un double ne peut pas garantir : que le contrat implémenté par
le vrai service correspond bien à ce que nos mappers attendent. Un champ
renommé ou un format de date modifié côté TMS se verra ici, et nulle part
ailleurs.

Ignorés si le TMS n'est pas lancé — ils ne doivent jamais bloquer la suite.
Pour les jouer :

    cd x_tms/server
    uv run --project . python -m uvicorn tms.api.app:app --port 8000
    uv run --project . python -c "from tms.cli import app; \
        app(['start-missions', '-s', 'deviation', '--speed', '120'])"
"""

import os

import httpx
import pytest

from services.mcp_server.connectors.base import TmsFailure
from services.mcp_server.connectors.dialect import load_named_dialect
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.mappers import PivotMapper
from services.mcp_server.snapshots import SnapshotService
from services.monitoring.rules.engine import evaluate_state_rules
from shared.mapped_dto import EventType, MissionStatus, ToolErrorCode

pytestmark = [pytest.mark.integration, pytest.mark.live_tms]

TMS_URL = os.environ.get("TMS_BASE_URL", "http://127.0.0.1:8000")

# Mapper du dialecte livré : les tests live vérifient que le contrat réel du
# TMS correspond bien aux correspondances déclarées.
MAPPER = PivotMapper(load_named_dialect("x_tms"))
TMS_API = f"{TMS_URL.rstrip('/')}/v1"


def tms_reachable() -> bool:
    try:
        return httpx.get(f"{TMS_API}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark.append(pytest.mark.skipif(not tms_reachable(), reason=f"TMS non joignable sur {TMS_API}"))


@pytest.fixture
async def connector():
    client = RestApiTmsConnector(TMS_API, retry_backoff_s=0.05)
    yield client
    await client.aclose()


class TestContratReel:
    async def test_heure_du_tms_lisible(self, connector):
        """Le TMS rend une date suffixée « Z » : le format doit être accepté."""
        moment = await connector.tms_time()
        assert moment.tzinfo is not None

    async def test_tous_les_statuts_de_mission_sont_reconnus(self, connector):
        """Un statut inconnu écarterait silencieusement des missions du suivi.

        Ignoré sur un TMS vide : c'est son état normal au repos, et non un
        défaut — `scripts/tms.sh demo <scénario>` le peuple.
        """
        missions = await connector.list_missions()
        if not missions:
            pytest.skip("TMS vide — lancer `scripts/tms.sh demo deviation`")

        for mission in missions:
            assert MAPPER.mission_status(mission["status"]) is not None, (
                f"statut non reconnu : {mission['status']!r}"
            )

    async def test_tous_les_types_devenements_sont_reconnus(self, connector):
        """Un type rangé sous `other` signale un vocabulaire qui a divergé."""
        missions = await connector.list_missions()
        inconnus: set[str] = set()

        for mission in missions[:5]:
            for raw in await connector.get_mission_events(mission["id"]):
                event_type, raw_type = MAPPER.event_type(raw["type"])
                if event_type is EventType.OTHER:
                    inconnus.add(str(raw_type))

        assert not inconnus, f"types d'événements non traduits : {sorted(inconnus)}"

    async def test_annuaire_resolvable_pour_chaque_role(self, connector):
        """F16 : chaque rôle cité par les règles livrées doit exister dans le TMS."""
        for role in ("ops_agent", "ops_supervisor", "ops_manager", "director"):
            users = await connector.list_users_by_role(role)
            assert users, f"aucun utilisateur pour le rôle {role!r}"
            assert all(user.get("management_level") for user in users)

    async def test_mission_inexistante_rend_mission_not_found(self, connector):
        with pytest.raises(TmsFailure) as caught:
            await connector.get_mission("msn_inexistante")
        assert caught.value.error.code is ToolErrorCode.MISSION_NOT_FOUND
        assert caught.value.retryable is False


class TestInstantaneReel:
    async def test_assemblage_sur_une_mission_en_cours(self, connector):
        """Le test décisif : un snapshot pivot complet, bâti sur de vraies réponses."""
        missions = await connector.list_missions(statuses=["in_progress"])
        if not missions:
            pytest.skip("aucune mission en cours — lancer `start-missions`")

        service = SnapshotService(connector, MAPPER)
        observed_at = await service.tms_time()
        snapshot = await service.build(missions[0]["id"], observed_at=observed_at)

        assert snapshot is not None
        assert snapshot.status is MissionStatus.IN_PROGRESS
        assert snapshot.observed_at == observed_at
        assert snapshot.reference
        assert snapshot.vehicle is not None, "véhicule non résolu"
        assert snapshot.driver is not None, "chauffeur non résolu"
        assert snapshot.loading is not None and snapshot.unloading is not None
        assert snapshot.events, "chronologie vide sur une mission en cours"
        # Les parties facultatives peuvent manquer légitimement ; en revanche
        # rien ne doit avoir échoué sur un TMS en bonne santé.
        assert snapshot.missing_parts == (), f"parties en échec : {snapshot.missing_parts}"

    async def test_cycle_complet_sur_le_tms_reel(self, connector):
        """La récolte F2 de bout en bout : heure TMS, missions, instantanés.

        Passe aussi sur un TMS vide : un cycle sans mission est un cycle
        valide, pas une anomalie.
        """
        batch = await SnapshotService(connector, MAPPER, mission_concurrency=4).collect_cycle()

        assert batch.failures == {}, f"missions illisibles : {batch.failures}"
        assert all(
            snapshot.status in (MissionStatus.PLANNED, MissionStatus.IN_PROGRESS)
            for snapshot in batch.snapshots
        )

    async def test_regles_livrees_evaluables_sur_le_tms_reel(self, connector):
        """La boucle complète, sans base de données : TMS → pivot → règles.

        Ce qui est vérifié n'est pas qu'une alerte se déclenche — cela dépend de
        l'état de la simulation — mais que le moteur évalue toutes les règles
        livrées sur de vraies données sans jamais lever.
        """
        from pathlib import Path

        from services.monitoring.rules.loader import load_rules

        ruleset = load_rules(Path("services/monitoring/rules/definitions"))
        batch = await SnapshotService(connector, MAPPER).collect_cycle()
        if not batch.snapshots:
            pytest.skip("aucune mission surveillée")

        for snapshot in batch.snapshots:
            evaluations = evaluate_state_rules(ruleset.rules, snapshot)
            assert len(evaluations) == len(ruleset.state_rules)
            for evaluation in evaluations:
                if evaluation.matched:
                    # Une alerte réelle doit porter un message rendu et une clé.
                    assert evaluation.title and evaluation.message
                    assert evaluation.deduplication_key
                    assert "{{" not in evaluation.message  # aucun gabarit non résolu


class TestLectureSeuleReelle:
    async def test_le_tms_refuse_lecriture(self, connector):
        """Garantie côté TMS, et non seulement côté client.

        Le TMS n'autorise que `GET`. Le vérifier ici documente la contrainte
        comme un fait vérifiable, et non comme une convention de notre code.
        """
        async with httpx.AsyncClient(base_url=TMS_API, timeout=5.0) as client:
            response = await client.post("/missions", json={"reference": "PIRATE"})
        assert response.status_code in (404, 405, 403, 422)
