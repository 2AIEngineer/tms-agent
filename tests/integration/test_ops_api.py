"""Tests de l'API d'exploitation.

Ce qui se joue ici est le contrôle d'accès autant que le contenu des réponses.
Trois propriétés, et ce sont elles qui décident si l'API peut être exposée sur
Internet :

  - **aucune route n'est ouverte par distraction.** Sans jeton, tout ce qui
    n'est pas la sonde de vivacité répond 401 ;
  - **le périmètre de l'appelant prime sur ses filtres.** Un opérateur ne peut
    pas élargir sa vue en passant un `tms_id` auquel il n'a pas accès, et une
    alerte hors périmètre est introuvable — pas « interdite », ce qui en
    révélerait l'existence ;
  - **l'identité vient du jeton, jamais du corps de la requête.** Poser une
    question au nom d'un collègue, et agir en son nom dans l'audit, doit être
    impossible.

L'application est montée sur la base de test réelle, avec son schéma migré :
une requête qui passerait sur un double et casserait sur PostgreSQL ne
m'apprendrait rien.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from services.agents.assistant.repository import ConversationRepository
from services.monitoring.alerts.repository import AlertRepository
from services.monitoring.state import MonitoringStateRepository, cycle_key
from services.ops_api.auth import principal_from_claims
from services.ops_api.config import OpsApiSettings
from services.ops_api.dependencies import AppState, principal
from services.ops_api.main import build_app
from services.ops_api.repository import OpsRepository
from services.ops_api.routers.sse import stream_alerts
from shared.mapped_dto import (
    ActionDecision,
    ActionKind,
    Alert,
    AlertStatus,
    AssistantAnswer,
    AssistantMode,
    ConversationMessage,
    MessageRole,
    PendingAction,
    PendingActionStatus,
    ResolutionReason,
    Severity,
)
from tests.factories import TMS_NOW

pytestmark = pytest.mark.integration

OPERATOR = "11111111-2222-3333-4444-555555555555"


class _ConnectedRequest:
    """Requête réduite à ce que le flux lui demande : est-elle encore là ?"""

    async def is_disconnected(self) -> bool:
        return False


class _StubOpsMcp:
    """Façade MCP réduite à la liste des TMS raccordés."""

    def __init__(self, tms_ids: list[str]) -> None:
        self._tms_ids = tms_ids

    async def connected_tms(self) -> list[str]:
        return list(self._tms_ids)


class StubAssistant:
    """Assistant scripté : les tests de l'API portent sur l'API."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.conversations: dict[uuid.UUID, list[ConversationMessage]] = {}
        self.waiting: dict[str, list[PendingAction]] = {}
        self.decisions: list[tuple[uuid.UUID, str, ActionDecision]] = []

    async def respond(self, request) -> AssistantAnswer:
        self.requests.append(request)
        return AssistantAnswer(
            conversation_id=request.conversation_id or uuid.uuid4(),
            answer="Réponse de test.",
            mode=request.mode,
            answered_at=datetime.now(UTC),
        )

    async def history(self, conversation_id, operator_id, *, limit=100):
        if operator_id != OPERATOR:
            return []
        return self.conversations.get(conversation_id, [])

    async def waiting_actions(self, operator_id, *, limit=20):
        return self.waiting.get(operator_id, [])

    async def decide(self, action_id, *, operator_id, decision):
        self.decisions.append((action_id, operator_id, decision))
        return AssistantAnswer(
            conversation_id=uuid.uuid4(),
            answer="Décision prise.",
            mode=AssistantMode.CHAT,
            answered_at=datetime.now(UTC),
        )


def make_app(database, *, roles=("ops_agent",), tms_ids=(), assistant=None, mcp=None):
    """Monte l'application avec un appelant connu et la base de test.

    L'authentification est remplacée par une dépendance, et non contournée dans
    le code de l'API : les routes voient exactement le même objet `Principal`
    qu'en production.
    """
    app = build_app(OpsApiSettings(auth_disabled=False))
    app.state.ops = AppState(
        settings=OpsApiSettings(auth_disabled=False),
        database=database,
        repository=OpsRepository(),
        assistant=assistant,
        mcp=mcp,
    )
    if roles is not None:
        caller = principal_from_claims({"oid": OPERATOR, "roles": list(roles)})
        if tms_ids:
            caller = type(caller)(
                operator_id=caller.operator_id,
                name=caller.name,
                roles=caller.roles,
                permissions=caller.permissions,
                tms_ids=tuple(tms_ids),
            )
        app.dependency_overrides[principal] = lambda: caller
    return app


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


async def seed_alert(
    database,
    *,
    severity: Severity = Severity.HIGH,
    tms_id: str = "x_tms",
    mission_id: str = "msn_1",
    status: AlertStatus = AlertStatus.OPEN,
    detected_at: datetime | None = None,
) -> uuid.UUID:
    alert = Alert(
        tms_id=tms_id,
        mission_id=mission_id,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=severity,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté.",
        observed_data={"deviation.current_offset_km": 7.2},
        deduplication_key=f"deviation:{tms_id}:{mission_id}:{severity.value}",
        detected_at=detected_at or TMS_NOW,
    )
    async with database.transaction() as connection:
        alert_id = await AlertRepository().insert_alert(connection, alert)
        assert alert_id is not None
        if status is not AlertStatus.OPEN:
            await AlertRepository().resolve(
                connection,
                [alert_id],
                resolved_at=TMS_NOW,
                reason=ResolutionReason.CONDITION_CLEARED,
            )
    return alert_id


class TestAccès:
    async def test_sonde_de_vivacite_sans_jeton(self, database):
        """Exiger un jeton ici ferait redémarrer le conteneur en boucle."""
        app = make_app(database, roles=None)

        async with client(app) as http:
            response = await http.get("/health")

        assert response.status_code == 200
        assert response.json() == {"ok": True, "database": "ok"}

    async def test_sans_jeton_tout_le_reste_est_refuse(self, database):
        app = make_app(database, roles=None)

        async with client(app) as http:
            for route in ("/alerts", "/alerts/summary", "/monitoring", "/missions"):
                response = await http.get(route)
                assert response.status_code == 401, route

    async def test_role_inconnu_refuse(self, database):
        """Un annuaire mal configuré doit fermer la porte, pas l'ouvrir."""
        app = make_app(database, roles=("role_inconnu",))

        async with client(app) as http:
            response = await http.get("/alerts")

        assert response.status_code == 403

    async def test_lecture_seule_peut_lire(self, database):
        app = make_app(database, roles=("ops_viewer",))

        async with client(app) as http:
            response = await http.get("/alerts")

        assert response.status_code == 200


class TestAlertes:
    async def test_tri_par_gravite_puis_fraicheur(self, database):
        """`order by severity` trierait par ordre alphabétique : inacceptable."""
        await seed_alert(database, severity=Severity.MEDIUM, mission_id="msn_m")
        await seed_alert(database, severity=Severity.CRITICAL, mission_id="msn_c")
        await seed_alert(database, severity=Severity.LOW, mission_id="msn_l")
        await seed_alert(database, severity=Severity.HIGH, mission_id="msn_h")
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/alerts")).json()

        assert [alert["severity"] for alert in body["alerts"]] == [
            "critical",
            "high",
            "medium",
            "low",
        ]
        assert body["total"] == 4

    async def test_filtre_par_gravite_minimale(self, database):
        await seed_alert(database, severity=Severity.LOW, mission_id="msn_l")
        await seed_alert(database, severity=Severity.HIGH, mission_id="msn_h")
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/alerts", params={"min_severity": "high"})).json()

        assert [alert["severity"] for alert in body["alerts"]] == ["high"]
        # Le comptage applique les mêmes filtres que la liste : sans cela, la
        # pagination ne tomberait jamais juste.
        assert body["total"] == 1

    async def test_filtre_par_statut(self, database):
        await seed_alert(database, mission_id="msn_ouverte")
        await seed_alert(database, mission_id="msn_close", status=AlertStatus.RESOLVED)
        app = make_app(database)

        async with client(app) as http:
            ouvertes = (await http.get("/alerts", params={"status": "open"})).json()

        assert [alert["mission_id"] for alert in ouvertes["alerts"]] == ["msn_ouverte"]

    async def test_perimetre_prime_sur_le_filtre_demande(self, database):
        """Un opérateur ne peut pas élargir sa vue en passant un `tms_id`."""
        await seed_alert(database, tms_id="x_tms")
        await seed_alert(database, tms_id="fleetlink")
        app = make_app(database, tms_ids=("x_tms",))

        async with client(app) as http:
            body = (await http.get("/alerts", params={"tms_id": "fleetlink"})).json()

        assert body["alerts"] == []
        assert body["total"] == 0

    async def test_detail_rassemble_tout_ce_qui_sy_rattache(self, database):
        alert_id = await seed_alert(database)
        app = make_app(database)

        async with client(app) as http:
            detail = (await http.get(f"/alerts/{alert_id}")).json()

        assert detail["id"] == str(alert_id)
        assert detail["observed_data"]["deviation.current_offset_km"] == 7.2
        # Les quatre volets sont présents, même vides : l'interface n'a pas à
        # distinguer « pas encore enquêté » de « champ absent de la réponse ».
        for key in ("investigation", "notifications", "driver_calls", "audit"):
            assert key in detail

    async def test_alerte_hors_perimetre_introuvable(self, database):
        """404 et non 403 : un 403 révélerait son existence."""
        alert_id = await seed_alert(database, tms_id="fleetlink")
        app = make_app(database, tms_ids=("x_tms",))

        async with client(app) as http:
            response = await http.get(f"/alerts/{alert_id}")

        assert response.status_code == 404

    async def test_synthese_par_tms_et_gravite(self, database):
        await seed_alert(database, severity=Severity.CRITICAL, tms_id="x_tms")
        await seed_alert(database, severity=Severity.HIGH, tms_id="x_tms")
        await seed_alert(database, severity=Severity.HIGH, tms_id="fleetlink")
        await seed_alert(database, mission_id="msn_close", status=AlertStatus.RESOLVED)
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/alerts/summary")).json()

        # Les alertes closes ne pèsent pas sur le bandeau d'exploitation.
        assert body["open_alerts"] == 3
        assert body["by_severity"] == {"critical": 1, "high": 2}
        assert body["by_tms"]["fleetlink"] == {"high": 1}


class TestAssistant:
    async def test_identite_prise_dans_le_jeton(self, database):
        """Poser une question au nom d'un collègue doit être impossible."""
        stub = StubAssistant()
        app = make_app(database, assistant=stub)

        async with client(app) as http:
            response = await http.post("/assistant", json={"message": "Où en est msn_1 ?"})

        assert response.status_code == 200
        assert stub.requests[0].operator_id == OPERATOR

    async def test_champ_didentite_refuse_dans_le_corps(self, database):
        """Le contrat est strict : un champ inattendu est rejeté, pas ignoré."""
        app = make_app(database, assistant=StubAssistant())

        async with client(app) as http:
            response = await http.post("/assistant", json={"message": "bonjour", "operator_id": "ops_autre"})

        assert response.status_code == 422

    async def test_perimetre_transmis_a_lassistant(self, database):
        stub = StubAssistant()
        app = make_app(database, assistant=stub, tms_ids=("x_tms",))

        async with client(app) as http:
            await http.post("/assistant", json={"message": "point", "mode": "report"})

        assert stub.requests[0].tms_ids == ("x_tms",)
        assert stub.requests[0].mode is AssistantMode.REPORT

    async def test_tms_raccordes_nommes_a_defaut_de_perimetre(self, database):
        """Mesuré : sans TMS nommé, le modèle local en invente un.

        Interrogé sans périmètre, il a produit `tms_id: "tms1"` au lieu
        d'appeler `list_tms`, et la lecture a échoué. Les énoncer coûte un
        appel et supprime la question.
        """
        stub = StubAssistant()
        app = make_app(database, assistant=stub, mcp=_StubOpsMcp(["x_tms", "fleetlink"]))

        async with client(app) as http:
            await http.post("/assistant", json={"message": "combien de missions ?"})

        assert stub.requests[0].tms_ids == ("x_tms", "fleetlink")

    async def test_sans_serveur_mcp_lassistant_se_debrouille(self, database):
        """L'absence de liste ne doit pas empêcher la question d'être posée."""
        stub = StubAssistant()
        app = make_app(database, assistant=stub, mcp=None)

        async with client(app) as http:
            response = await http.post("/assistant", json={"message": "bonjour"})

        assert response.status_code == 200
        assert stub.requests[0].tms_ids == ()

    async def test_agir_exige_le_role(self, database):
        """Un consentement ne crée pas un rôle."""
        stub = StubAssistant()
        app = make_app(database, roles=("ops_viewer",), assistant=stub)

        async with client(app) as http:
            response = await http.post("/assistant", json={"message": "Clos l'alerte", "allow_actions": True})

        assert response.status_code == 403
        assert not stub.requests

    async def test_agir_exige_aussi_la_demande_explicite(self, database):
        """Et un rôle suffisant ne vaut pas consentement (F26)."""
        stub = StubAssistant()
        app = make_app(database, roles=("ops_supervisor",), assistant=stub)

        async with client(app) as http:
            await http.post("/assistant", json={"message": "Clos l'alerte"})

        assert stub.requests[0].allow_actions is False

    async def test_assistant_indisponible_rend_503(self, database):
        """Sans serveur MCP ni inférence, les alertes restent consultables."""
        app = make_app(database, assistant=None)

        async with client(app) as http:
            indispo = await http.post("/assistant", json={"message": "bonjour"})
            alertes = await http.get("/alerts")

        assert indispo.status_code == 503
        assert alertes.status_code == 200

    async def test_historique_dun_collegue_invisible(self, database):
        stub = StubAssistant()
        conversation_id = uuid.uuid4()
        stub.conversations[conversation_id] = [
            ConversationMessage(role=MessageRole.OPERATOR, content="confidentiel")
        ]
        app = make_app(database, assistant=stub)

        async with client(app) as http:
            mienne = (await http.get(f"/assistant/conversations/{conversation_id}")).json()

        assert mienne["count"] == 1

        autre = make_app(database, assistant=stub)
        autre.dependency_overrides[principal] = lambda: principal_from_claims(
            {"oid": "autre-operateur", "roles": ["ops_agent"]}
        )
        async with client(autre) as http:
            vol = (await http.get(f"/assistant/conversations/{conversation_id}")).json()

        assert vol["messages"] == []


class TestValidationDesActions:
    """La validation humaine, vue de l'API.

    Deux clés sont exigées pour agir, et ce n'est pas une redondance : le rôle
    dit *qui peut*, la validation dit *quoi exactement*. Un rôle suffisant ne
    vaut pas consentement, et un consentement ne crée pas un rôle.
    """

    def _pending(self) -> PendingAction:
        return PendingAction(
            id=uuid.uuid4(),
            kind=ActionKind.CLOSE_ALERT,
            status=PendingActionStatus.PROPOSED,
            summary="Clore l'alerte « Dérive d'itinéraire » (mission msn_1)",
            reason="Détour autorisé par le client.",
            conversation_id=uuid.uuid4(),
            arguments={"alert_id": str(uuid.uuid4())},
        )

    async def test_liste_de_ce_qui_attend(self, database):
        """« Qu'attend-on de moi ? » — la question posée en ouvrant l'écran."""
        stub = StubAssistant()
        attente = self._pending()
        stub.waiting[OPERATOR] = [attente]
        app = make_app(database, assistant=stub)

        async with client(app) as http:
            body = (await http.get("/assistant/pending")).json()

        assert body["count"] == 1
        # L'opérateur lit ce qui va s'exécuter, pas un identifiant.
        assert "Dérive d'itinéraire" in body["pending_actions"][0]["summary"]

    async def test_validation_exige_le_role(self, database):
        stub = StubAssistant()
        app = make_app(database, roles=("ops_viewer",), assistant=stub)

        async with client(app) as http:
            response = await http.post(f"/assistant/actions/{uuid.uuid4()}/confirm")

        assert response.status_code == 403
        assert not stub.decisions

    async def test_validation_transmise_avec_lidentite_du_jeton(self, database):
        """Valider au nom d'un collègue doit être impossible."""
        stub = StubAssistant()
        app = make_app(database, assistant=stub)
        action_id = uuid.uuid4()

        async with client(app) as http:
            response = await http.post(f"/assistant/actions/{action_id}/confirm")

        assert response.status_code == 200
        assert stub.decisions == [(action_id, OPERATOR, ActionDecision.CONFIRM)]

    async def test_refus_transmis(self, database):
        stub = StubAssistant()
        app = make_app(database, assistant=stub)
        action_id = uuid.uuid4()

        async with client(app) as http:
            await http.post(f"/assistant/actions/{action_id}/reject")

        assert stub.decisions == [(action_id, OPERATOR, ActionDecision.REJECT)]

    async def test_sans_jeton_aucune_validation(self, database):
        app = make_app(database, roles=None, assistant=StubAssistant())

        async with client(app) as http:
            for route in ("/assistant/pending", f"/assistant/actions/{uuid.uuid4()}/confirm"):
                method = http.get if route.endswith("pending") else http.post
                assert (await method(route)).status_code == 401, route


class TestRapportsPlanifies:
    async def test_rapports_lus_par_tout_le_monde(self, database):
        """Produits sans que personne ne les demande, lus par tous (F24)."""
        from services.agents.config import get_settings as get_agent_settings

        repository = ConversationRepository()
        operator = get_agent_settings().report_operator_id
        async with database.transaction() as connection:
            conversation_id = await repository.open(
                connection,
                operator_id=operator,
                title="Rapport de situation",
                mode=AssistantMode.REPORT,
            )
            await repository.append(
                connection,
                conversation_id,
                [
                    ConversationMessage(role=MessageRole.OPERATOR, content="Fais le point."),
                    ConversationMessage(
                        role=MessageRole.ASSISTANT, content="3 missions, 1 écart à surveiller."
                    ),
                ],
            )
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/assistant/reports")).json()

        assert body["count"] == 1
        # Le texte du rapport, et non la suite des lectures qui l'ont produit.
        assert body["reports"][0]["report"] == "3 missions, 1 écart à surveiller."

    async def test_conversations_dopérateurs_exclues(self, database):
        """La liste ne doit pas montrer les questions des collègues."""
        repository = ConversationRepository()
        async with database.transaction() as connection:
            await repository.open(
                connection,
                operator_id="ops_awa",
                title="ma question",
                mode=AssistantMode.CHAT,
            )
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/assistant/reports")).json()

        assert body["count"] == 0


class TestTempsReel:
    async def test_flux_annonce_son_etablissement_puis_les_alertes(self, database):
        """Sans l'événement `ready`, l'interface ne sait pas si elle est connectée.

        La route est appelée directement et son flux consommé tel quel : le
        transport ASGI de httpx rassemble la réponse entière avant de la rendre
        et ne peut donc pas consommer un flux sans fin. Le tester à travers lui
        bloquerait indéfiniment — ce qui est arrivé en écrivant ce test.
        """
        settings = OpsApiSettings(auth_disabled=False, sse_poll_interval_s=0.05, sse_keepalive_s=0.01)
        app_state = AppState(settings=settings, database=database, repository=OpsRepository())
        caller = principal_from_claims({"oid": OPERATOR, "roles": ["ops_agent"]})

        response = await stream_alerts(
            _ConnectedRequest(),  # type: ignore[arg-type]
            caller,
            database,
            OpsRepository(),
            app_state,
        )
        assert response.media_type == "text/event-stream"
        # Sans cet en-tête, un mandataire met le flux en tampon et le temps
        # réel disparaît.
        assert response.headers["x-accel-buffering"] == "no"

        recu: list[str] = []
        async with asyncio.timeout(10):
            async for chunk in response.body_iterator:
                text = chunk if isinstance(chunk, str) else chunk.decode()
                for line in text.splitlines():
                    if line.startswith("event:"):
                        recu.append(line.removeprefix("event: ").strip())
                if recu == ["ready"]:
                    # Semée pendant que le flux tourne : c'est tout l'objet du
                    # curseur, qui ne doit rien manquer.
                    await seed_alert(database, severity=Severity.CRITICAL)
                if "alert" in recu:
                    break

        assert recu[0] == "ready"
        assert "alert" in recu

    async def test_curseur_sur_lhorloge_du_serveur(self, database):
        """Une alerte datée dans le futur du TMS ne doit pas être manquée.

        Le curseur porte sur `created_at` et non `detected_at` : en simulation
        accélérée, l'heure TMS avance plus vite que le temps réel, et un
        curseur posé dessus sauterait des alertes.
        """
        repository = OpsRepository()
        avant = datetime.now(UTC) - timedelta(seconds=1)
        await seed_alert(database, detected_at=TMS_NOW + timedelta(hours=5))

        async with database.connection() as connection:
            nouvelles = await repository.new_alerts_since(connection, after=avant)

        assert len(nouvelles) == 1


class TestEtatDuSysteme:
    async def test_battement_par_tms(self, database):
        """Un TMS silencieux doit se voir à côté de ceux qui tournent."""
        state = MonitoringStateRepository()
        async with database.transaction() as connection:
            await state.record_cycle(connection, {"tms_id": "x_tms", "missions": 3}, key=cycle_key("x_tms"))
            await state.record_cycle(
                connection,
                {"tms_id": "fleetlink", "failed": True, "failure_reason": "TMS injoignable"},
                key=cycle_key("fleetlink"),
            )
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/monitoring")).json()

        cycles = {item["tms_id"]: item for item in body["cycles"]}
        assert cycles["x_tms"]["cycle"]["missions"] == 3
        assert cycles["fleetlink"]["cycle"]["failed"] is True

    async def test_file_denquetes_visible(self, database):
        from services.agents.investigator.repository import InvestigationRepository

        await seed_alert(database, severity=Severity.CRITICAL)
        async with database.transaction() as connection:
            await InvestigationRepository().claim_batch(connection, min_severity=Severity.MEDIUM, limit=4)
        app = make_app(database)

        async with client(app) as http:
            body = (await http.get("/monitoring")).json()

        assert body["investigations"] == {"running": 1}

    async def test_mode_developpement_annonce(self, database):
        """Une API lancée sans authentification doit le dire dans sa réponse."""
        app = make_app(database)
        app.state.ops.settings = OpsApiSettings(auth_disabled=True)

        async with client(app) as http:
            body = (await http.get("/monitoring")).json()

        assert body["auth_disabled"] is True


class TestMissions:
    async def test_sans_serveur_mcp_rend_503(self, database):
        """Le TMS devient illisible ; les alertes en base restent consultables."""
        app = make_app(database, mcp=None)

        async with client(app) as http:
            missions = await http.get("/missions")
            alertes = await http.get("/alerts")

        assert missions.status_code == 503
        assert alertes.status_code == 200


class TestLectureSeule:
    async def test_aucune_route_ecrit_en_base(self, database):
        """L'API ne doit offrir aucun chemin d'écriture hors de l'assistant."""
        app = make_app(database, assistant=StubAssistant())

        chemins = {
            (route.path, tuple(sorted(route.methods or ())))
            for route in _routes(app)
            if route.path != "/openapi.json"
        }
        ecritures = {
            (path, methods) for path, methods in chemins if {"POST", "PUT", "PATCH", "DELETE"} & set(methods)
        }

        # Les seules écritures passent par l'assistant et ses garde-fous :
        # poser une demande, valider une action préparée, la refuser — chacune
        # en deux variantes, diffusée ou non, servies par le **même** code.
        # Toute autre route en écriture contournerait la validation humaine et
        # l'audit.
        assert ecritures == {
            ("/assistant", ("POST",)),
            ("/assistant/stream", ("POST",)),
            ("/assistant/actions/{action_id}/confirm", ("POST",)),
            ("/assistant/actions/{action_id}/confirm/stream", ("POST",)),
            ("/assistant/actions/{action_id}/reject", ("POST",)),
            ("/assistant/actions/{action_id}/reject/stream", ("POST",)),
        }


def _routes(app) -> list[Any]:
    """Aplatit les routes, y compris celles des routeurs inclus paresseusement."""
    collected: list[Any] = []
    pending = list(app.routes)
    while pending:
        route = pending.pop()
        inner = getattr(route, "original_router", None)
        if inner is not None:
            pending.extend(inner.routes)
            continue
        if hasattr(route, "methods"):
            collected.append(route)
    return collected
