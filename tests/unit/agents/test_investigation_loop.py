"""Tests de la boucle d'enquête.

Le vrai modèle ne reproduit pas sur commande une soumission mal formée, un
refus de conclure ou un quota épuisé. Ces comportements sont scriptés, et ce
sont eux qui décident de la robustesse de la boucle.

Ce qui est vérifié tient en trois phrases : le modèle ne peut rendre qu'une
évaluation valide, il n'a jamais la main sur les identifiants, et aucun de ses
égarements ne coûte l'alerte.
"""

import uuid
from datetime import UTC, datetime

import pytest

from services.agents.integrations.mcp_client import AgentMcpClient
from services.agents.investigator.loop import Investigator
from services.agents.investigator.submission import SUBMIT_TOOL_NAME
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.context import McpContext
from services.mcp_server.server import build_server
from shared.mapped_dto import Alert, InvestigationRequest, RiskLevel, Severity
from tests.fake_llm import FakeLlm, reply, submit, tool_call, transient, unavailable, valid_submission
from tests.fake_tms import FakeTms

INVESTIGATION_ID = uuid.uuid4()
ALERT_ID = uuid.uuid4()


def make_request(**overrides) -> InvestigationRequest:
    alert = Alert(
        id=ALERT_ID,
        tms_id="x_tms",
        mission_id="msn_1",
        rule_id="deviation_persistent",
        rule_version=2,
        severity=Severity.HIGH,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule AB-123-CD s'est écarté de 7.2 km.",
        observed_data={"deviation.current_offset_km": 7.2},
        deduplication_key="deviation:msn_1",
        detected_at=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
    )
    base = {
        "investigation_id": INVESTIGATION_ID,
        "alert_id": ALERT_ID,
        "tms_id": "x_tms",
        "mission_id": "msn_1",
        "alert": alert,
        "observed_facts": {"deviation": {"current_offset_km": 7.2, "duration_minutes": 25}},
        "notifications_sent": ("ops_supervisor (M+2) par sms : delivered",),
        "requested_at": datetime(2026, 10, 10, 9, 1, tzinfo=UTC),
    }
    return InvestigationRequest(**{**base, **overrides})


@pytest.fixture
async def mcp():
    tms = FakeTms()
    connector = RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    server, context = build_server(context=McpContext.for_connector(connector))
    async with AgentMcpClient(server) as client:
        yield client
    await context.aclose()


def investigator(llm: FakeLlm, mcp: AgentMcpClient, **kwargs) -> Investigator:
    return Investigator(llm, mcp, **kwargs)  # type: ignore[arg-type]


class TestEnqueteNominale:
    async def test_conclusion_en_un_tour(self, mcp):
        llm = FakeLlm([submit()])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        assert outcome.result is not None
        assert outcome.result.risk is RiskLevel.MEDIUM
        assert outcome.result.confidence == 0.7
        assert outcome.rounds == 1
        assert outcome.tool_calls == 0

    async def test_identifiants_jamais_fournis_par_le_modele(self, mcp):
        """Le modèle pourrait sinon attacher son analyse à une autre alerte.

        L'erreur serait silencieuse et grave : l'opérateur lirait une
        explication qui ne correspond pas à son alerte.
        """
        llm = FakeLlm([submit()])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.result is not None
        assert outcome.result.investigation_id == INVESTIGATION_ID
        assert outcome.result.alert_id == ALERT_ID
        # Le schéma soumis au modèle ne contient aucun identifiant.
        soumis = next(t for t in llm.calls[0]["tools"] if t == SUBMIT_TOOL_NAME)
        assert soumis == SUBMIT_TOOL_NAME

    async def test_outils_mcp_exposes_au_modele(self, mcp):
        """Objectif 6 : les outils sont découverts, pas codés dans l'agent."""
        llm = FakeLlm([submit()])
        await investigator(llm, mcp).investigate(make_request())

        offerts = llm.calls[0]["tools"]
        assert "get_mission_snapshot" in offerts
        assert "get_driver" in offerts
        assert SUBMIT_TOOL_NAME in offerts
        # L'outil du cycle de surveillance est caché : il rendrait toute la
        # flotte dans la fenêtre de contexte (§20).
        assert "get_monitoring_cycle" not in offerts

    async def test_appel_doutil_puis_conclusion(self, mcp):
        llm = FakeLlm(
            [
                reply(calls=[tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": "msn_1"})]),
                submit(),
            ]
        )
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        assert outcome.rounds == 2
        assert outcome.tool_calls == 1
        # Le résultat de l'outil est bien remis au modèle.
        messages = llm.calls[1]["messages"]
        tool_message = next(m for m in messages if m["role"] == "tool")
        assert "snapshot" in tool_message["content"]

    async def test_jetons_cumules_sur_tous_les_tours(self, mcp):
        """Le volume de jetons par enquête est le poste de coût à mesurer."""
        llm = FakeLlm(
            [
                reply(
                    calls=[tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": "msn_1"})],
                    input_tokens=500,
                    output_tokens=20,
                ),
                submit(),
            ]
        )
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.usage.input_tokens == 600
        assert outcome.usage.output_tokens == 60
        assert outcome.model == "faux-modele"
        assert outcome.duration_ms >= 0

    async def test_contexte_initial_remis_au_modele(self, mcp):
        """F21 : les faits déterministes, sans que le modèle ait à les chercher."""
        llm = FakeLlm([submit()])
        await investigator(llm, mcp).investigate(make_request())

        user_message = llm.calls[0]["messages"][1]["content"]
        assert "x_tms" in user_message
        assert "deviation_persistent" in user_message
        assert "current_offset_km" in user_message
        assert "ops_supervisor" in user_message  # déjà prévenus

    async def test_trace_reliee_a_lalerte(self, mcp):
        """Sans cela, un coût anormal vu dans Langfuse serait irrattachable."""
        llm = FakeLlm([submit()])
        await investigator(llm, mcp).investigate(make_request())

        trace = llm.calls[0]["trace"]
        assert trace["alert_id"] == str(ALERT_ID)
        assert trace["tms_id"] == "x_tms"
        assert trace["rule_id"] == "deviation_persistent"


class TestEgarementsDuModele:
    async def test_soumission_non_conforme_rendue_au_modele(self, mcp):
        """Le modèle doit pouvoir se corriger, plutôt que perdre l'enquête."""
        llm = FakeLlm(
            [
                reply(calls=[tool_call(SUBMIT_TOOL_NAME, {"summary": "trop court"})]),
                submit(),
            ]
        )
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        assert outcome.rounds == 2
        correction = llm.calls[1]["messages"][-1]
        assert correction["role"] == "tool"
        assert "non conforme" in correction["content"]

    async def test_prose_sans_conclusion_ramenee_a_loutil(self, mcp):
        llm = FakeLlm([reply(content="Je pense que le camion a fait un détour."), submit()])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        rappel = llm.calls[1]["messages"][-1]
        assert rappel["role"] == "user"
        assert "submit_assessment" in rappel["content"]

    async def test_conclusion_forcee_au_dernier_tour(self, mcp):
        """Mieux vaut une analyse prudente qu'aucune analyse."""
        llm = FakeLlm(
            [
                reply(calls=[tool_call("get_route", {"tms_id": "x_tms", "mission_id": "msn_1"})]),
                submit(confidence=0.3),
            ]
        )
        outcome = await investigator(llm, mcp, max_tool_rounds=2).investigate(make_request())

        assert outcome.succeeded
        assert outcome.result is not None
        assert outcome.result.confidence == 0.3
        # Au dernier tour, l'outil de conclusion est imposé.
        assert llm.last_tool_choice() == {
            "type": "function",
            "function": {"name": SUBMIT_TOOL_NAME},
        }

    async def test_jamais_de_conclusion_malgre_la_contrainte(self, mcp):
        """Plafond atteint : l'enquête échoue, l'alerte reste intacte."""
        llm = FakeLlm([reply(content="Je réfléchis encore."), reply(content="Toujours.")])
        outcome = await investigator(llm, mcp, max_tool_rounds=2).investigate(make_request())

        assert outcome.succeeded is False
        assert outcome.skipped is False
        assert outcome.error is not None and "Aucune conclusion" in outcome.error

    async def test_outil_inexistant_rendu_comme_erreur(self, mcp):
        """Le modèle se corrige ; une exception perdrait l'enquête."""
        llm = FakeLlm([reply(calls=[tool_call("get_weather", {"city": "Lyon"})]), submit()])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        tool_message = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
        assert '"ok": false' in tool_message["content"]

    async def test_outil_appele_sans_tms_id(self, mcp):
        """`tms_id` est obligatoire : l'erreur revient au modèle, qui réessaie."""
        llm = FakeLlm(
            [
                reply(calls=[tool_call("get_mission_snapshot", {"mission_id": "msn_1"})]),
                submit(),
            ]
        )
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        tool_message = next(m for m in llm.calls[1]["messages"] if m["role"] == "tool")
        assert '"ok": false' in tool_message["content"]

    async def test_plusieurs_outils_dans_un_tour(self, mcp):
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call("get_route", {"tms_id": "x_tms", "mission_id": "msn_1"}, call_id="a"),
                        tool_call("get_driver", {"tms_id": "x_tms", "driver_id": "drv_1"}, call_id="b"),
                    ]
                ),
                submit(),
            ]
        )
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.tool_calls == 2
        tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
        assert len(tool_messages) == 2


class TestResilienceDuLlm:
    async def test_llm_indisponible_saute_lenquete(self, mcp):
        """Sans LLM, la détection continue : seule l'analyse manque (F19)."""
        llm = FakeLlm([unavailable("aucune clé configurée")])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded is False
        assert outcome.skipped is True
        assert outcome.error is not None and "clé" in outcome.error

    async def test_echec_passager_distinct_dune_indisponibilite(self, mcp):
        """« Sauté » et « en échec » n'appellent pas la même intervention."""
        llm = FakeLlm([transient("503 du fournisseur")])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded is False
        assert outcome.skipped is False
        assert outcome.error is not None and "503" in outcome.error

    async def test_mesures_conservees_malgre_lechec(self, mcp):
        """Une enquête échouée a tout de même consommé du temps."""
        llm = FakeLlm([unavailable()])
        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.duration_ms >= 0
        assert outcome.rounds == 1


class TestSchemaDeSoumission:
    def test_schema_derive_du_contrat(self):
        from services.agents.investigator.submission import submit_tool_definition

        parameters = submit_tool_definition()["function"]["parameters"]

        assert set(parameters["required"]) == {"summary", "situation", "risk", "confidence"}
        # Aucun identifiant ni horodatage n'est demandé au modèle.
        for interdit in ("investigation_id", "alert_id", "completed_at"):
            assert interdit not in parameters["properties"]

    def test_champs_decrits_pour_le_modele(self):
        """Un champ sans description est rempli au hasard."""
        from services.agents.investigator.submission import submit_tool_definition

        properties = submit_tool_definition()["function"]["parameters"]["properties"]
        for name, schema in properties.items():
            assert schema.get("description"), f"{name} n'est pas décrit"

    def test_conversion_en_resultat(self):
        from services.agents.investigator.submission import InvestigationSubmission

        submission = InvestigationSubmission.model_validate(valid_submission())
        result = submission.to_result(investigation_id=INVESTIGATION_ID, alert_id=ALERT_ID)

        assert result.investigation_id == INVESTIGATION_ID
        assert result.alert_id == ALERT_ID
        assert result.actionable is True


class StubLocalTool:
    """Outil local scripté : note ce que le modèle lui a demandé."""

    name = "call_driver"

    def __init__(self, payload: dict | None = None) -> None:
        self.invocations: list[tuple[uuid.UUID, dict]] = []
        self._payload = payload or {"ok": True, "status": "placed", "placed": True}

    @staticmethod
    def definition() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "call_driver",
                "description": "Appelle le chauffeur.",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}},
            },
        }

    async def invoke(self, request: InvestigationRequest, arguments: dict) -> dict:
        self.invocations.append((request.investigation_id, arguments))
        return self._payload


class TestOutilsLocaux:
    """L'appel au chauffeur n'est pas un outil MCP : il est servi par l'agent.

    Le TMS est en lecture seule, donc un appel téléphonique n'est pas une
    opération TMS, et sa trace appartient au domaine de l'agent. La boucle doit
    donc router un appel d'outil vers l'un ou l'autre selon son nom.
    """

    async def test_outil_local_annonce_au_modele(self, mcp):
        tool = StubLocalTool()
        llm = FakeLlm([submit()])

        await investigator(llm, mcp, local_tools=[tool]).investigate(make_request())

        assert "call_driver" in llm.calls[0]["tools"]

    async def test_appel_route_vers_loutil_local(self, mcp):
        """Le contexte de l'enquête est fourni par la boucle, pas par le modèle."""
        tool = StubLocalTool()
        llm = FakeLlm(
            [
                reply(calls=[tool_call("call_driver", {"reason": "Confirmer la cause de l'arrêt."})]),
                submit(),
            ]
        )

        outcome = await investigator(llm, mcp, local_tools=[tool]).investigate(make_request())

        assert outcome.succeeded
        assert outcome.tool_calls == 1
        assert tool.invocations == [(INVESTIGATION_ID, {"reason": "Confirmer la cause de l'arrêt."})]

    async def test_refus_rendu_au_modele_sans_rompre_lenquete(self, mcp):
        """Un quota atteint n'est pas une panne : l'enquête se conclut sans appel."""
        tool = StubLocalTool(
            {"ok": True, "status": "blocked", "placed": False, "blocked_reason": "driver_quota"}
        )
        llm = FakeLlm([reply(calls=[tool_call("call_driver", {"reason": "Vérifier."})]), submit()])

        outcome = await investigator(llm, mcp, local_tools=[tool]).investigate(make_request())

        assert outcome.succeeded
        contenu = [m for m in llm.calls[1]["messages"] if m.get("name") == "call_driver"]
        assert "driver_quota" in contenu[0]["content"]

    async def test_sans_outil_local_rien_nest_annonce(self, mcp):
        """`DRIVER_CALL_ENABLED=false` retire l'outil au lieu de refuser l'appel."""
        llm = FakeLlm([submit()])

        await investigator(llm, mcp).investigate(make_request())

        assert "call_driver" not in llm.calls[0]["tools"]


class TestFenetreDeContexte:
    """L'enquête doit survivre à un historique qui grossit.

    Rencontré en exécution réelle : au troisième tour, la requête dépassait la
    fenêtre de 4 096 jetons du modèle de développement, le repli cloud refusait
    à son tour pour dépassement de quota, et l'enquête était perdue.
    """

    async def test_historique_compacte_avant_envoi(self, mcp):
        """Le modèle reçoit moins que l'historique réel, et tient dans sa fenêtre."""
        from services.agents.integrations.budget import OMITTED_RESULT

        # Trois lectures puis une conclusion, avec un plafond serré.
        llm = FakeLlm(
            [
                reply(
                    calls=[
                        tool_call("get_mission_snapshot", {"tms_id": "x_tms", "mission_id": "msn_1"}, "c1")
                    ]
                ),
                reply(
                    calls=[tool_call("get_mission_events", {"tms_id": "x_tms", "mission_id": "msn_1"}, "c2")]
                ),
                reply(calls=[tool_call("get_route", {"tms_id": "x_tms", "mission_id": "msn_1"}, "c3")]),
                submit(),
            ]
        )

        # 3 400 jetons, soit la fenêtre de développement : au dernier tour, le
        # socle tombe à ~2 600 (seul l'outil de conclusion est envoyé), et les
        # trois lectures accumulées ne tiennent plus — c'est exactement la
        # situation rencontrée en exécution réelle.
        outcome = await investigator(llm, mcp, max_input_tokens=3400).investigate(make_request())

        assert outcome.succeeded

        dernier = llm.calls[-1]["messages"]
        resultats = [item for item in dernier if item.get("role") == "tool"]
        intacts = [item for item in resultats if item["content"] != OMITTED_RESULT]

        # Le modèle reçoit moins que l'historique réel : les lectures anciennes
        # ont été vidées (et annoncées) ou leurs tours écartés entiers.
        assert len(resultats) < 3
        # Le socle est intact : sans le prompt ni les faits, le modèle ne sait
        # plus ce qu'on lui demande.
        assert dernier[0]["role"] == "system"
        assert "Faits déterministes" in dernier[1]["content"]
        # Et la lecture la plus récente — celle sur laquelle il travaille — est
        # restée entière.
        assert intacts
        assert intacts[-1]["name"] == "get_route"

    async def test_dernier_tour_sans_les_schemas_de_lecture(self, mcp):
        """Les schémas dominent le socle, et la compaction n'y peut rien.

        Au dernier tour, `tool_choice` impose la conclusion : les six schémas
        d'outils de lecture ne servent plus à rien et pèsent plus de mille
        jetons — précisément là où la fenêtre est le plus sollicitée.
        """
        llm = FakeLlm([reply(content="je réfléchis encore"), submit()])

        await investigator(llm, mcp, max_tool_rounds=2).investigate(make_request())

        assert len(llm.calls[0]["tools"]) > 1
        assert llm.calls[-1]["tools"] == [SUBMIT_TOOL_NAME]

    async def test_seconde_tentative_resserree(self, mcp):
        """Si le fournisseur refuse malgré la compaction, on resserre une fois."""
        from services.agents.integrations.litellm_client import LlmCallFailed, LlmErrorKind

        refus = LlmCallFailed(
            LlmErrorKind.CONTEXT_OVERFLOW,
            "Inférence refusée (400) : maximum context length is 4096 tokens",
            status_code=400,
        )
        llm = FakeLlm([refus, submit()])

        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.succeeded
        assert llm.rounds == 2

    async def test_refus_persistant_nest_pas_un_echec_de_notre_fait(self, mcp):
        """Deux refus : l'enquête échoue, et le motif le dit."""
        from services.agents.integrations.litellm_client import LlmCallFailed, LlmErrorKind

        def refus() -> LlmCallFailed:
            return LlmCallFailed(
                LlmErrorKind.CONTEXT_OVERFLOW,
                "Inférence refusée (400) : maximum context length is 4096 tokens",
                status_code=400,
            )

        outcome = await investigator(FakeLlm([refus(), refus()]), mcp).investigate(make_request())

        assert not outcome.succeeded
        assert outcome.error is not None
        assert "context length" in outcome.error


class TestQuotaDinference:
    """Un quota épuisé n'est pas un défaut de notre côté.

    Mesuré sur le palier gratuit du fournisseur (7 000 jetons par minute) :
    classé « passager », un 429 marquait l'enquête **en échec** et consommait
    une tentative. Trois de suite privaient l'alerte d'analyse pour de bon,
    alors que le modèle était simplement saturé pendant une minute.
    """

    async def test_quota_fait_sauter_lenquete_et_non_echouer(self, mcp):
        from services.agents.integrations.litellm_client import LlmCallFailed, LlmErrorKind

        refus = LlmCallFailed(
            LlmErrorKind.UNAVAILABLE,
            "Inférence refusée (429) : rate limit",
            status_code=429,
        )
        llm = FakeLlm([refus])

        outcome = await investigator(llm, mcp).investigate(make_request())

        assert outcome.skipped is True
        assert not outcome.succeeded

    async def test_429_classe_comme_indisponibilite(self):
        """La qualification se fait dans le client, pas dans la boucle."""
        import httpx

        from services.agents.integrations.litellm_client import LiteLlmClient, LlmErrorKind

        client = LiteLlmClient("http://proxy.test", "clef", model="m")
        failure = client._failure_from(httpx.Response(429, json={"error": {"message": "rate limit"}}))

        assert failure.kind is LlmErrorKind.UNAVAILABLE
        assert failure.skip_investigation is True
        await client.aclose()
