"""Tests des outils MCP, à travers le protocole réel.

Un vrai client MCP parlant au serveur en mémoire : les schémas d'entrée, la
sérialisation des résultats et le contrat d'erreur sont donc exercés tels que
l'agent les verra. Les appeler en Python nu testerait les fonctions, pas les
outils.

Le TMS est remplacé par son double, pour provoquer à volonté les pannes qu'un
vrai TMS ne produit pas sur commande.
"""

import json
from typing import Any

import pytest
from fastmcp import Client

from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from tests.fake_tms import TMS_NOW, FakeTms, event_payload, iso, mission_payload

pytestmark = pytest.mark.integration


@pytest.fixture
def tms() -> FakeTms:
    return FakeTms()


@pytest.fixture
async def mcp_client(tms: FakeTms):
    """Client MCP branché sur le serveur en mémoire, lui-même sur le faux TMS."""
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with Client(server) as client:
        yield client
    await context.aclose()


async def call(client: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    """Appelle un outil et rend sa charge utile désérialisée.

    `tms_id` est ajouté par défaut : il est obligatoire sur tous les outils, et
    le répéter dans chaque test n'apprendrait rien de plus.
    """
    arguments.setdefault("tms_id", "x_tms")
    if tool == "list_tms":
        arguments.pop("tms_id", None)
    result = await client.call_tool(tool, arguments)
    if result.structured_content is not None:
        payload = result.structured_content
        # FastMCP enveloppe un résultat non modélisé sous `result`.
        return payload.get("result", payload) if isinstance(payload, dict) else payload
    return json.loads(result.content[0].text)


class TestCatalogue:
    async def test_outils_exposes_par_capacite_metier(self, mcp_client):
        """§19-20 : des capacités métier, pas une façade CRUD sur le TMS."""
        tools = {tool.name for tool in await mcp_client.list_tools()}
        assert tools == {
            "get_active_missions",
            "get_mission_snapshot",
            "get_mission_events",
            "get_tms_time",
            "get_current_position",
            "get_position_history",
            "get_route",
            "get_driver",
            "get_vehicle",
            "resolve_recipients",
            "get_monitoring_cycle",
            "list_tms",
        }

    async def test_chaque_outil_est_decrit(self, mcp_client):
        """Un outil sans description est inutilisable par un LLM."""
        for tool in await mcp_client.list_tools():
            assert tool.description, f"{tool.name} n'a pas de description"
            assert len(tool.description) > 40, f"{tool.name} : description trop courte"

    async def test_instructions_du_serveur_cadrent_lusage(self, mcp_client):
        instructions = mcp_client.instructions or ""
        assert "Lecture seule" in instructions
        assert "retryable" in instructions


class TestMissions:
    async def test_missions_actives_avec_lheure_du_tms(self, mcp_client, tms):
        tms.missions = [
            mission_payload("msn_1", status="in_progress"),
            mission_payload("msn_2", status="completed"),
            mission_payload("msn_3", status="planned"),
        ]
        payload = await call(mcp_client, "get_active_missions")

        assert payload["ok"] is True
        assert payload["tms_time"] == iso(TMS_NOW)
        assert {mission["mission_id"] for mission in payload["missions"]} == {"msn_1", "msn_3"}

    async def test_instantane_complet(self, mcp_client):
        payload = await call(mcp_client, "get_mission_snapshot", mission_id="msn_1")

        snapshot = payload["snapshot"]
        assert payload["ok"] is True
        assert snapshot["mission_id"] == "msn_1"
        assert snapshot["status"] == "in_progress"
        assert snapshot["snapshot_complete"] is True
        assert snapshot["vehicle"]["plate"] == "AB-123-CD"
        assert snapshot["deviation"]["current_offset_km"] == 7.234
        assert snapshot["eta"]["delay_minutes"] == 90.0
        assert len(snapshot["events"]) == 3

    async def test_instantane_partiel_signale(self, mcp_client, tms):
        """L'agent doit savoir qu'une absence n'est pas une information."""
        tms.fail("/missions/msn_1/deviation", 503)
        payload = await call(mcp_client, "get_mission_snapshot", mission_id="msn_1")

        snapshot = payload["snapshot"]
        assert snapshot["snapshot_complete"] is False
        assert snapshot["missing_parts"] == ["deviation"]
        assert snapshot["deviation"] is None

    async def test_chronologie_avec_distance_derivee(self, mcp_client, tms):
        tms.missions[0]["events"] = [event_payload("evt_1", "arrived_delivery", TMS_NOW, lat=45.80, lon=4.90)]
        payload = await call(mcp_client, "get_mission_events", mission_id="msn_1")

        event = payload["events"][0]
        assert event["type"] == "arrived_delivery"
        assert 5 < event["distance_to_target_km"] < 9


class TestSuivi:
    async def test_heure_et_simulation(self, mcp_client):
        payload = await call(mcp_client, "get_tms_time")
        assert payload["tms_time"] == iso(TMS_NOW)
        assert payload["simulation"]["speed"] == 60.0

    async def test_position_courante(self, mcp_client):
        payload = await call(mcp_client, "get_current_position", vehicle_id="veh_1")
        assert payload["status"] == "moving"
        assert payload["position"]["speed_kmh"] == 82.0

    async def test_historique_tronque_par_la_fin(self, mcp_client, tms):
        """Les points récents sont les utiles : c'est le début qui est coupé."""
        tms.positions = [
            {"lat": 47.0 + index * 0.001, "lon": 3.5, "recorded_at": iso(TMS_NOW)} for index in range(300)
        ]
        payload = await call(mcp_client, "get_position_history", vehicle_id="veh_1", limit=1000)

        assert payload["count"] == 200
        assert payload["truncated"] is True
        assert payload["positions"][-1]["lat"] == pytest.approx(47.0 + 299 * 0.001)

    async def test_itineraire_absent_rendu_explicitement(self, mcp_client, tms):
        """Sans itinéraire, toute conclusion sur une dérive est sans fondement."""
        tms.route = None
        payload = await call(mcp_client, "get_route", mission_id="msn_1")
        assert payload["ok"] is True
        assert payload["route"] is None


class TestParticipants:
    async def test_chauffeur(self, mcp_client):
        payload = await call(mcp_client, "get_driver", driver_id="drv_1")
        assert payload["driver"]["name"] == "Awa Traoré"
        assert payload["current_mission_id"] == "msn_1"

    async def test_destinataires_dun_role(self, mcp_client):
        payload = await call(mcp_client, "resolve_recipients", role="ops_supervisor", channel="sms")
        assert payload["count"] == 1
        assert payload["recipients"][0]["address"] == "+33600000000"
        assert payload["recipients"][0]["reachable"] is True

    async def test_chauffeur_de_la_mission(self, mcp_client):
        payload = await call(
            mcp_client, "resolve_recipients", role="driver", channel="call", mission_id="msn_1"
        )
        assert payload["recipients"][0]["user_id"] == "drv_1"

    async def test_role_driver_sans_mission_refuse(self, mcp_client):
        payload = await call(mcp_client, "resolve_recipients", role="driver", channel="call")
        assert payload["recipients"] == []
        assert "mission_id" in payload["reason"]

    async def test_canal_inconnu_explique(self, mcp_client):
        payload = await call(mcp_client, "resolve_recipients", role="ops_agent", channel="pigeon")
        assert payload["recipients"] == []
        assert "canal inconnu" in payload["reason"]


class TestContratDerreur:
    async def test_mission_inexistante_est_un_fait_metier(self, mcp_client):
        """L'agent doit conclure, pas réessayer."""
        payload = await call(mcp_client, "get_mission_snapshot", mission_id="msn_999")

        assert payload["ok"] is False
        assert payload["error"]["code"] == "MISSION_NOT_FOUND"
        assert payload["error"]["retryable"] is False

    async def test_tms_en_panne_est_un_incident_technique(self, mcp_client, tms):
        """L'agent doit réessayer, et surtout ne rien conclure sur la mission."""
        tms.fail("/missions/msn_1", 503)
        payload = await call(mcp_client, "get_mission_snapshot", mission_id="msn_1")

        assert payload["ok"] is False
        assert payload["error"]["code"] == "TMS_UNAVAILABLE"
        assert payload["error"]["retryable"] is True

    async def test_delai_depasse(self, mcp_client, tms):
        tms.timeout("/health")
        payload = await call(mcp_client, "get_tms_time")

        assert payload["error"]["code"] == "TMS_TIMEOUT"
        assert payload["error"]["retryable"] is True

    async def test_erreur_rendue_comme_donnee_et_non_levee(self, mcp_client, tms):
        """Une exception ne laisserait à l'agent qu'un message en texte libre.

        Rendre l'erreur comme donnée lui permet de lire `code` et `retryable`,
        donc de distinguer un fait métier d'un incident technique (§21).
        """
        tms.fail("/missions/msn_1", 500)
        result = await mcp_client.call_tool(
            "get_mission_snapshot", {"tms_id": "x_tms", "mission_id": "msn_1"}
        )

        assert result.is_error is False  # l'appel d'outil a réussi
        payload = await call(mcp_client, "get_mission_snapshot", mission_id="msn_1")
        assert payload["ok"] is False


class TestLectureSeule:
    async def test_aucun_outil_nemet_decriture_vers_le_tms(self, mcp_client, tms):
        """Contrainte 1 du cahier des charges, vérifiée sur le trafic réel."""
        await call(mcp_client, "get_active_missions")
        await call(mcp_client, "get_mission_snapshot", mission_id="msn_1")
        await call(mcp_client, "get_mission_events", mission_id="msn_1")
        await call(mcp_client, "get_current_position", vehicle_id="veh_1")
        await call(mcp_client, "get_position_history", vehicle_id="veh_1")
        await call(mcp_client, "get_route", mission_id="msn_1")
        await call(mcp_client, "get_driver", driver_id="drv_1")
        await call(mcp_client, "get_vehicle", vehicle_id="veh_1")
        await call(mcp_client, "resolve_recipients", role="ops_agent", channel="in_app")

        assert set(tms.methods) == {"GET"}
