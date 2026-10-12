"""Tests des deux lectures locales de l'enquêteur (F21).

Ni l'une ni l'autre ne passe par MCP, et c'est le point : l'historique de
l'orchestrateur est **le nôtre**, et la réévaluation des règles appelle le
moteur même qui a créé l'alerte. Le serveur MCP est la frontière du TMS, pas
celle de notre base.

Ce qui est vérifié :

  - l'historique dit la **récurrence** — combien de fois, closes comment, et
    surtout ce qu'un opérateur a déjà décidé ;
  - la réévaluation répond « la condition tient-elle encore ? », et distingue
    une situation qui dure d'une alerte qui semble infondée.

Les règles employées sont celles livrées dans `definitions/` : des règles
inventées pour le test ne diraient rien de ce que fera la production.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from services.agents.local_tools import AgentHistoryTool, RuleCheckTool
from services.agents.local_tools.history import HISTORY_TOOL_NAME
from services.agents.local_tools.rules import RULES_TOOL_NAME
from services.monitoring.alerts.repository import AlertRepository
from shared.mapped_dto import (
    Alert,
    AlertStatus,
    InvestigationRequest,
    InvestigationResult,
    ResolutionReason,
    RiskLevel,
    Severity,
)
from shared.utils.serialization import to_jsonable
from tests.factories import MISSION_ID, TMS_NOW, make_snapshot

pytestmark = pytest.mark.integration


class StubRules:
    """Dépôt de règles figé : les règles livrées, sans relecture de disque."""

    def __init__(self, ruleset) -> None:
        self._ruleset = ruleset
        self.reloads = 0

    def reload_if_changed(self):
        self.reloads += 1
        return self._ruleset


class StubMcp:
    """Serveur MCP réduit à l'instantané de mission."""

    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot

    async def call(self, name: str, arguments: dict):
        from services.agents.integrations.mcp_client import ToolResult

        payload = {"ok": True, "snapshot": to_jsonable(self._snapshot) if self._snapshot else None}
        return ToolResult(name=name, payload=payload)


async def seed_alert(
    database,
    *,
    rule_id: str = "deviation_persistent",
    severity: Severity = Severity.HIGH,
    detected_at: datetime | None = None,
    status: AlertStatus = AlertStatus.OPEN,
    reason: ResolutionReason = ResolutionReason.CONDITION_CLEARED,
) -> uuid.UUID:
    moment = detected_at or TMS_NOW
    alert = Alert(
        tms_id="x_tms",
        mission_id=MISSION_ID,
        rule_id=rule_id,
        rule_version=2,
        severity=severity,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté.",
        deduplication_key=f"{rule_id}:x_tms:{MISSION_ID}:{moment.isoformat()}",
        detected_at=moment,
    )
    async with database.transaction() as connection:
        alert_id = await AlertRepository().insert_alert(connection, alert)
        assert alert_id is not None
        if status is not AlertStatus.OPEN:
            await AlertRepository().resolve(
                connection, [alert_id], resolved_at=moment + timedelta(minutes=20), reason=reason
            )
    return alert_id


def make_request(alert_id: uuid.UUID, *, rule_id: str = "deviation_persistent") -> InvestigationRequest:
    alert = Alert(
        id=alert_id,
        tms_id="x_tms",
        mission_id=MISSION_ID,
        rule_id=rule_id,
        rule_version=2,
        severity=Severity.HIGH,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté de 7,9 km.",
        deduplication_key=f"{rule_id}:x_tms:{MISSION_ID}",
        detected_at=TMS_NOW,
    )
    return InvestigationRequest(
        investigation_id=uuid.uuid4(),
        alert_id=alert_id,
        tms_id="x_tms",
        mission_id=MISSION_ID,
        alert=alert,
        observed_facts={},
        notifications_sent=(),
        requested_at=datetime.now(UTC),
    )


class TestHistorique:
    async def test_aucun_identifiant_a_fournir(self):
        """La mission est celle de l'enquête : le modèle ne la choisit pas."""
        parameters = AgentHistoryTool.definition()["function"]["parameters"]

        assert set(parameters["properties"]) == {"limit"}
        assert parameters["required"] == []
        assert AgentHistoryTool.definition()["function"]["name"] == HISTORY_TOOL_NAME

    async def test_recurrence_comptee_pour_le_modele(self, database):
        """« Cette mission a déjà dérivé trois fois » ne se compte pas à la main."""
        for minutes in (240, 180, 120):
            await seed_alert(
                database,
                detected_at=TMS_NOW - timedelta(minutes=minutes),
                status=AlertStatus.RESOLVED,
            )
        current = await seed_alert(database)
        tool = AgentHistoryTool(database)

        payload = await tool.invoke(make_request(current), {})

        assert payload["ok"] is True
        assert payload["alert_count"] == 4
        assert payload["alerts_per_rule"] == {"deviation_persistent": 4}
        # L'alerte en cours est signalée : sinon le modèle la compte comme un
        # antécédent de lui-même.
        assert [alert["is_current"] for alert in payload["alerts"]] == [True, False, False, False]

    async def test_cloture_et_son_motif_rendus(self, database):
        await seed_alert(
            database,
            detected_at=TMS_NOW - timedelta(hours=2),
            status=AlertStatus.RESOLVED,
            reason=ResolutionReason.CONDITION_CLEARED,
        )
        current = await seed_alert(database)

        payload = await AgentHistoryTool(database).invoke(make_request(current), {})

        passee = payload["alerts"][-1]
        assert passee["status"] == AlertStatus.RESOLVED.value
        assert passee["resolution_reason"] == ResolutionReason.CONDITION_CLEARED.value

    async def test_decision_doperateur_rendue(self, database):
        """Le fait le plus utile de tout l'historique."""
        current = await seed_alert(database)
        async with database.transaction() as connection:
            await AlertRepository().append_audit(
                connection,
                [
                    {
                        "action": "operator_decision",
                        "tms_id": "x_tms",
                        "mission_id": MISSION_ID,
                        "alert_id": current,
                        "actor": "ops_awa",
                        "detail": {"decision": "Détour autorisé par le client."},
                        "occurred_at": TMS_NOW,
                    }
                ],
            )

        payload = await AgentHistoryTool(database).invoke(make_request(current), {})

        assert len(payload["operator_decisions"]) == 1
        assert "Détour autorisé" in payload["operator_decisions"][0]["detail"]["decision"]
        assert payload["operator_decisions"][0]["actor"] == "ops_awa"

    async def test_enquetes_reduites_a_leur_conclusion(self, database):
        """Un résultat complet pèserait plus que tout le reste réuni."""
        from services.agents.investigator.repository import InvestigationRepository

        current = await seed_alert(database, severity=Severity.CRITICAL)
        repository = InvestigationRepository()
        async with database.transaction() as connection:
            claimed = await repository.claim_batch(connection, min_severity=Severity.MEDIUM, limit=1)
            await repository.complete(
                connection,
                claimed[0],
                InvestigationResult(
                    investigation_id=claimed[0],
                    alert_id=current,
                    summary="Détour par la N7 pour éviter un bouchon.",
                    situation="Écart de 7,9 km, véhicule en mouvement.",
                    probable_cause="Contournement d'un ralentissement.",
                    risk=RiskLevel.LOW,
                    confidence=0.8,
                    completed_at=TMS_NOW,
                ),
                model="faux-modele",
                input_tokens=100,
                output_tokens=40,
                duration_ms=1200,
            )

        payload = await AgentHistoryTool(database).invoke(make_request(current), {})

        enquete = payload["investigations"][0]
        assert enquete["summary"].startswith("Détour par la N7")
        assert enquete["risk"] == "low"
        # Ni preuves, ni actions recommandées : elles n'éclairent pas la
        # récurrence et rempliraient la fenêtre de contexte.
        assert "evidence" not in enquete

    async def test_appel_refuse_compte_aussi(self, database):
        """Un refus dit au modèle qu'un quota est consommé."""
        from services.agents.operations.driver_call import (
            CallGuardrails,
            CallRequest,
            DriverCallService,
        )

        class _Channel:
            name = "stub"

            @property
            def channel(self):
                from shared.mapped_dto import NotificationChannel

                return NotificationChannel.CALL

            async def send(self, message, *, sent_at):
                from shared.channels import SendResult

                return SendResult.ok(self.name)

        current = await seed_alert(database)
        service = DriverCallService(_Channel(), guardrails=CallGuardrails())
        call = CallRequest(
            tms_id="x_tms",
            mission_id=MISSION_ID,
            driver_id="drv_1",
            reason="Motif beaucoup trop court",
            tms_now=TMS_NOW,
            severity=Severity.LOW,
            alert_id=current,
            phone="+33600000001",
        )
        async with database.transaction() as connection:
            await service.place(connection, call)

        payload = await AgentHistoryTool(database).invoke(make_request(current), {})

        assert payload["driver_calls"][0]["status"] == "blocked"
        assert payload["driver_calls"][0]["blocked_reason"] == "severity_too_low"

    async def test_autre_mission_invisible(self, database):
        """L'historique est celui de cette mission, pas de la flotte."""
        await seed_alert(database, detected_at=TMS_NOW - timedelta(hours=1), status=AlertStatus.RESOLVED)
        current = await seed_alert(database)
        alert = Alert(
            tms_id="x_tms",
            mission_id="msn_autre",
            rule_id="eta_delay",
            rule_version=1,
            severity=Severity.MEDIUM,
            category="delay",
            title="Retard",
            message="Retard estimé.",
            deduplication_key="eta:x_tms:msn_autre",
            detected_at=TMS_NOW,
        )
        async with database.transaction() as connection:
            await AlertRepository().insert_alert(connection, alert)

        payload = await AgentHistoryTool(database).invoke(make_request(current), {})

        assert set(payload["alerts_per_rule"]) == {"deviation_persistent"}

    async def test_limite_hors_bornes_refusee(self, database):
        current = await seed_alert(database)

        payload = await AgentHistoryTool(database).invoke(make_request(current), {"limit": 500})

        assert payload["ok"] is False


class TestReevaluationDesRegles:
    async def test_aucun_parametre(self):
        """Le modèle ne choisit pas la règle qu'il vérifie."""
        definition = RuleCheckTool.definition()["function"]

        assert definition["parameters"]["properties"] == {}
        assert definition["name"] == RULES_TOOL_NAME

    async def test_condition_toujours_vraie(self, database, shipped_rules):
        """La situation dure : la règle de l'alerte déclenche encore."""
        snapshot = make_snapshot(deviation_km=7.9, deviation_minutes=25.0)
        tool = RuleCheckTool(StubRules(shipped_rules), StubMcp(snapshot))  # type: ignore[arg-type]
        current = await seed_alert(database)

        payload = await tool.invoke(make_request(current), {})

        assert payload["evaluated"] is True
        assert payload["this_rule_still_matches"] is True
        assert "deviation_persistent" in [item["rule_id"] for item in payload["matched"]]
        # Les valeurs qui déclenchent sont rendues : le modèle doit pouvoir
        # citer son chiffre, pas seulement un verdict.
        declenchee = next(item for item in payload["matched"] if item["rule_id"] == "deviation_persistent")
        assert declenchee["observed_data"]

    async def test_condition_resorbee(self, database, shipped_rules):
        """Elle ne déclenche plus : la clôture automatique va suivre (F13)."""
        snapshot = make_snapshot(deviation_km=0.2, deviation_minutes=1.0)
        tool = RuleCheckTool(StubRules(shipped_rules), StubMcp(snapshot))  # type: ignore[arg-type]
        current = await seed_alert(database)

        payload = await tool.invoke(make_request(current), {})

        assert payload["this_rule_still_matches"] is False
        assert "deviation_persistent" in payload["not_matched"]

    async def test_mission_illisible_ne_conclut_rien(self, database, shipped_rules):
        tool = RuleCheckTool(StubRules(shipped_rules), StubMcp(None))  # type: ignore[arg-type]
        current = await seed_alert(database)

        payload = await tool.invoke(make_request(current), {})

        assert payload["evaluated"] is False
        assert "Ne rien conclure" in payload["note"]

    async def test_regles_relues_a_chaud(self, database, shipped_rules):
        """Un seuil modifié depuis la détection doit s'appliquer ici aussi (F8)."""
        rules = StubRules(shipped_rules)
        tool = RuleCheckTool(rules, StubMcp(make_snapshot()))  # type: ignore[arg-type]
        current = await seed_alert(database)

        await tool.invoke(make_request(current), {})

        assert rules.reloads == 1

    async def test_heure_de_reference_du_tms(self, database, shipped_rules):
        """L'évaluation se juge sur l'horloge du TMS, jamais la locale (F3)."""
        snapshot = make_snapshot(observed_at=TMS_NOW)
        tool = RuleCheckTool(StubRules(shipped_rules), StubMcp(snapshot))  # type: ignore[arg-type]
        current = await seed_alert(database)

        payload = await tool.invoke(make_request(current), {})

        assert payload["tms_time"] == TMS_NOW.isoformat()
