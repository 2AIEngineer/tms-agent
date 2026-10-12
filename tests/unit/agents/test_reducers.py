"""Tests de la réduction des résultats d'outils.

Ce module existe à cause d'un défaut constaté en conditions réelles : la
troncature par nombre de caractères avait coupé un instantané de mission
**avant son champ `deviation`**, c'est-à-dire exactement la réponse que
l'opérateur demandait. Le modèle, correctement, a répondu qu'il ne pouvait pas
savoir.

Ce qui est vérifié ici tient en une phrase : les champs qui portent une
décision survivent, les longs tableaux sont résumés, et ce qui a été retiré est
annoncé.
"""

import json

from services.agents.integrations.reducers import (
    EVENT_LIST_COUNT,
    SNAPSHOT_EVENT_COUNT,
    reduce_payload,
)
from shared.mapped_dto import MissionSnapshot
from shared.utils.serialization import to_jsonable
from tests.factories import make_event, make_snapshot

# Mesuré sur une mission réelle du TMS simulé : l'itinéraire planifié pesait
# 11 026 caractères, contre 301 pour l'écart à cet itinéraire.
MEASURED_ROUTE_CHARS = 11026


def _snapshot_payload(**kwargs) -> dict:
    snapshot = to_jsonable(make_snapshot(**kwargs))
    snapshot["planned_route"] = {
        "waypoints": [{"lat": 33.0 + index / 1000, "lon": -7.0} for index in range(250)],
        "distance_km": 240.0,
    }
    return {"ok": True, "snapshot": snapshot}


class TestContratPreserve:
    """L'instantané réduit doit rester un instantané **valide**.

    Défaut trouvé sur une exécution réelle de l'enquêteur : une clé
    d'annotation glissée dans `snapshot` le rendait invalide au regard du
    contrat pivot, qui interdit tout champ inattendu. Le journal disait
    « instantané non conforme : enquête sur les seuls faits » et la
    réévaluation des règles devenait impossible — le même objet étant lu par le
    modèle *et* par notre code.
    """

    def test_linstantane_reduit_se_valide(self):
        payload = _snapshot_payload(deviation_km=7.16, deviation_minutes=11.3, delay_minutes=12.0)

        reduced = reduce_payload("get_mission_snapshot", payload)

        validated = MissionSnapshot.model_validate(reduced["snapshot"])
        assert validated.deviation is not None
        assert validated.deviation.current_offset_km == 7.16
        assert validated.planned_route is not None

    def test_les_omissions_vivent_hors_de_linstantane(self):
        """Annoncées dans l'enveloppe : le modèle les lit, le contrat tient."""
        events = [make_event(event_id=f"evt_{index}") for index in range(20)]
        payload = _snapshot_payload(events=events)

        reduced = reduce_payload("get_mission_snapshot", payload)

        assert "reduction" in reduced
        assert reduced["reduction"]["events_omitted"] == 20 - SNAPSHOT_EVENT_COUNT
        assert "get_mission_events" in reduced["reduction"]["events_note"]
        # Rien d'étranger n'a été ajouté dans l'instantané lui-même.
        assert set(reduced["snapshot"]) <= set(MissionSnapshot.model_fields)

    def test_evenements_laisses_intacts(self):
        """Un événement amputé serait invalide : c'est leur nombre qui pèse."""
        payload = _snapshot_payload(events=[make_event(event_id="evt_1")])

        reduced = reduce_payload("get_mission_snapshot", payload)

        assert reduced["snapshot"]["events"] == payload["snapshot"]["events"]

    def test_litineraire_reste_un_itineraire(self):
        payload = _snapshot_payload()

        reduced = reduce_payload("get_mission_snapshot", payload)
        route = reduced["snapshot"]["planned_route"]

        assert len(route["waypoints"]) == 2
        assert route["distance_km"] == 240.0
        assert reduced["reduction"]["route_waypoints_omitted"] == 248
        MissionSnapshot.model_validate(reduced["snapshot"])

    def test_itineraire_deja_court_inchange(self):
        payload = _snapshot_payload()
        payload["snapshot"]["planned_route"] = {
            "waypoints": [{"lat": 33.0, "lon": -7.0}, {"lat": 31.0, "lon": -8.0}],
            "distance_km": 240.0,
        }

        reduced = reduce_payload("get_mission_snapshot", payload)

        assert "reduction" not in reduced or "route_waypoints_omitted" not in reduced["reduction"]


class TestInstantane:
    def test_les_champs_de_decision_survivent(self):
        """Le défaut constaté : `deviation` doit rester lisible."""
        payload = _snapshot_payload(deviation_km=7.16, deviation_minutes=11.3, delay_minutes=12.0)

        reduced = reduce_payload("get_mission_snapshot", payload)
        rendered = json.dumps(reduced, ensure_ascii=False)

        assert reduced["snapshot"]["deviation"]["current_offset_km"] == 7.16
        assert reduced["snapshot"]["eta"]["delay_minutes"] == 12.0
        assert reduced["snapshot"]["status"] == "in_progress"
        assert "current_offset_km" in rendered

    def test_litineraire_est_resume(self):
        """11 000 caractères d'itinéraire chassaient le champ utile."""
        payload = _snapshot_payload()
        avant = len(json.dumps(payload["snapshot"]["planned_route"], ensure_ascii=False))

        reduced = reduce_payload("get_mission_snapshot", payload)
        route = reduced["snapshot"]["planned_route"]

        assert avant > 5000
        assert len(json.dumps(route, ensure_ascii=False)) < 400
        # Les extrémités suffisent à raisonner sur un tracé.
        assert route["waypoints"][0]["lat"] == 33.0
        assert route["distance_km"] == 240.0

    def test_chronologie_plafonnee_en_nombre(self):
        """C'est le nombre d'événements qui pèse, pas leur contenu."""
        events = [make_event(event_id=f"evt_{index}") for index in range(20)]
        payload = _snapshot_payload(events=events)

        reduced = reduce_payload("get_mission_snapshot", payload)

        assert len(reduced["snapshot"]["events"]) == SNAPSHOT_EVENT_COUNT

    def test_les_derniers_evenements_sont_gardes(self):
        """Une chronologie se lit par la fin : c'est le dernier qui date un arrêt."""
        events = [make_event(event_id=f"evt_{index}") for index in range(10)]
        payload = _snapshot_payload(events=events)

        reduced = reduce_payload("get_mission_snapshot", payload)

        assert [event["id"] for event in reduced["snapshot"]["events"]] == [
            f"evt_{index}" for index in range(5, 10)
        ]

    def test_instantane_reduit_tient_dans_la_fenetre(self):
        """Un cas normal ne doit plus jamais rencontrer la troncature."""
        from services.agents.integrations.mcp_client import MAX_TOOL_RESULT_CHARS, ToolResult

        payload = _snapshot_payload(deviation_km=7.16, deviation_minutes=11.3)
        reduced = reduce_payload("get_mission_snapshot", payload)

        rendered = ToolResult("get_mission_snapshot", reduced).as_message_content(
            max_chars=MAX_TOOL_RESULT_CHARS
        )

        assert '"truncated": true' not in rendered
        assert "current_offset_km" in rendered


class TestAutresOutils:
    def test_chronologie_plafonnee(self):
        payload = {
            "ok": True,
            "count": 40,
            "events": [to_jsonable(make_event(event_id=f"e{i}")) for i in range(40)],
        }

        reduced = reduce_payload("get_mission_events", payload)

        assert len(reduced["events"]) == EVENT_LIST_COUNT
        assert reduced["events_omitted"] == 40 - EVENT_LIST_COUNT

    def test_positions_echantillonnees(self):
        payload = {"ok": True, "count": 100, "positions": [{"lat": i, "lon": 0} for i in range(100)]}

        reduced = reduce_payload("get_position_history", payload)

        assert len(reduced["positions"]) == 6
        assert reduced["positions_omitted"] == 94
        # Le total réel reste lisible : c'est lui qui dit si le véhicule bouge.
        assert reduced["count"] == 100

    def test_peu_de_positions_laissees_intactes(self):
        payload = {"ok": True, "count": 4, "positions": [{"lat": i, "lon": 0} for i in range(4)]}

        assert reduce_payload("get_position_history", payload) == payload

    def test_une_erreur_arrive_intacte(self):
        """C'est l'erreur qui apprend au modèle comment se corriger."""
        payload = {
            "ok": False,
            "error": {"code": "MISSION_NOT_FOUND", "message": "inconnue", "retryable": False},
        }

        assert reduce_payload("get_mission_snapshot", payload) == payload

    def test_outil_sans_reducteur_inchange(self):
        """Un outil inconnu ne doit pas être amputé au hasard."""
        payload = {"ok": True, "tms": [{"id": "x_tms"}]}

        assert reduce_payload("list_tms", payload) == payload
