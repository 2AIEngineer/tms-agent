"""Tests de l'appel au chauffeur, la seule action du système vers l'extérieur.

Ce qui se joue ici n'est pas qu'un appel puisse partir — c'est qu'il puisse
être **refusé**, et que le refus soit tracé. Un chauffeur rappelé par une
enquête rejouée, ou appelé quatre fois pour la même mission, serait un défaut
visible par le client avant de l'être par nous.

Les garde-fous sont vérifiés sur la base réelle : les quotas s'appuient sur des
index partiels (`status = 'placed'`), et un test qui les simulerait en mémoire
ne dirait rien de ce qui se passe en production.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from database.schema import driver_calls
from services.agents.investigator.repository import InvestigationRepository
from services.agents.operations.driver_call import (
    MIN_REASON_LENGTH,
    BlockedReason,
    CallGuardrails,
    CallRequest,
    CallStatus,
    DriverCallService,
)
from services.agents.operations.tool import CALL_TOOL_NAME, DriverCallTool
from services.monitoring.alerts.repository import AlertRepository
from shared.channels import Message, SendResult
from shared.mapped_dto import (
    Alert,
    InvestigationRequest,
    NotificationChannel,
    Severity,
)
from shared.utils.serialization import to_jsonable
from tests.factories import DRIVER_ID, MISSION_ID, TMS_NOW, make_snapshot

pytestmark = pytest.mark.integration

REASON = "Confirmer la raison de l'arrêt et l'heure de reprise estimée."


class RecordingChannel:
    """Canal d'appel qui note ce qui est parti, sans rien envoyer."""

    name = "recording"

    def __init__(self, *, delivered: bool = True) -> None:
        self.sent: list[Message] = []
        self._delivered = delivered

    @property
    def channel(self) -> NotificationChannel:
        return NotificationChannel.CALL

    async def send(self, message: Message, *, sent_at: datetime) -> SendResult:
        self.sent.append(message)
        if not self._delivered:
            return SendResult.transient_failure(self.name, "fournisseur injoignable")
        return SendResult.ok(self.name, reference="call_ref_1")


def make_request(
    *,
    reason: str = REASON,
    severity: Severity = Severity.HIGH,
    alert_id: uuid.UUID | None = None,
    driver_id: str = DRIVER_ID,
    mission_id: str = MISSION_ID,
    tms_now: datetime = TMS_NOW,
    phone: str | None = "+33600000001",
) -> CallRequest:
    # `alert_id` laissé vide par défaut : la table porte une clé étrangère vers
    # les alertes, et la plupart de ces tests n'ont pas d'alerte à faire exister
    # — ils portent sur les quotas, qui ne la regardent pas. La clé
    # d'idempotence devient alors propre à la demande, ce qui est exactement le
    # cas d'un appel hors alerte (F26).
    return CallRequest(
        tms_id="x_tms",
        mission_id=mission_id,
        driver_id=driver_id,
        reason=reason,
        tms_now=tms_now,
        severity=severity,
        alert_id=alert_id,
        phone=phone,
    )


async def rows(database) -> list[Any]:
    async with database.connection() as connection:
        result = await connection.execute(select(driver_calls).order_by(driver_calls.c.created_at))
        return list(result.mappings())


async def seed_alert(database, severity: Severity = Severity.HIGH, *, mission_id: str = MISSION_ID) -> Alert:
    alert = Alert(
        tms_id="x_tms",
        mission_id=mission_id,
        rule_id="deviation_persistent",
        rule_version=2,
        severity=severity,
        category="route_deviation",
        title="Dérive d'itinéraire",
        message="Le véhicule s'est écarté.",
        deduplication_key=f"deviation:x_tms:{mission_id}",
        detected_at=TMS_NOW,
    )
    async with database.transaction() as connection:
        alert_id = await AlertRepository().insert_alert(connection, alert)
    assert alert_id is not None
    return alert.model_copy(update={"id": alert_id})


async def seed_investigation(database, alert: Alert) -> uuid.UUID:
    """Réclame l'enquête de cette alerte, comme le worker le ferait.

    Une vraie ligne d'enquête et non un identifiant inventé : la table des
    appels porte une clé étrangère vers elle, précisément pour qu'un appel soit
    toujours rattachable à ce qui l'a décidé.
    """
    async with database.transaction() as connection:
        claimed = await InvestigationRepository().claim_batch(
            connection, min_severity=Severity.MEDIUM, limit=1
        )
    assert claimed, "l'alerte semée devrait être éligible à l'enquête"
    return claimed[0]


def make_investigation_request(alert: Alert, investigation_id: uuid.UUID) -> InvestigationRequest:
    return InvestigationRequest(
        investigation_id=investigation_id,
        alert_id=alert.id,
        tms_id=alert.tms_id,
        mission_id=alert.mission_id,
        alert=alert,
        observed_facts={},
        notifications_sent=(),
        requested_at=datetime.now(UTC),
    )


async def place(database, service: DriverCallService, request: CallRequest):
    async with database.transaction() as connection:
        return await service.place(connection, request)


class TestAppelPasse:
    async def test_appel_trace_et_remis_au_canal(self, database):
        channel = RecordingChannel()
        service = DriverCallService(channel)

        outcome = await place(database, service, make_request())

        assert outcome.placed
        assert len(channel.sent) == 1
        assert channel.sent[0].address == "+33600000001"
        assert channel.sent[0].body == REASON

        stored = await rows(database)
        assert len(stored) == 1
        assert stored[0]["status"] == CallStatus.PLACED.value
        assert stored[0]["blocked_reason"] is None
        # L'heure enregistrée est celle du TMS : les quotas doivent suivre une
        # simulation accélérée, pas l'horloge de la machine.
        assert stored[0]["requested_at"] == TMS_NOW
        assert stored[0]["placed_at"] == TMS_NOW

    async def test_echec_du_fournisseur_trace_sans_lever(self, database):
        service = DriverCallService(RecordingChannel(delivered=False))

        outcome = await place(database, service, make_request())

        assert outcome.status is CallStatus.FAILED
        stored = await rows(database)
        assert stored[0]["status"] == CallStatus.FAILED.value
        assert stored[0]["failure_reason"]
        assert stored[0]["placed_at"] is None


class TestIdempotence:
    async def test_meme_alerte_un_seul_appel(self, database):
        """Une enquête rejouée ne rappelle pas le chauffeur."""
        channel = RecordingChannel()
        service = DriverCallService(channel)
        alert_id = (await seed_alert(database)).id

        first = await place(database, service, make_request(alert_id=alert_id))
        second = await place(database, service, make_request(alert_id=alert_id))

        assert first.placed
        assert second.status is CallStatus.BLOCKED
        assert second.blocked_reason is BlockedReason.ALREADY_CALLED
        # Un seul appel réellement passé, mais deux lignes : le refus est tracé.
        assert len(channel.sent) == 1
        stored = await rows(database)
        assert len(stored) == 2
        assert {row["status"] for row in stored} == {
            CallStatus.PLACED.value,
            CallStatus.BLOCKED.value,
        }

    async def test_alerte_differente_appel_autorise(self, database):
        channel = RecordingChannel()
        # Cooldown neutralisé : ce test porte sur l'idempotence, pas sur lui.
        service = DriverCallService(channel, guardrails=CallGuardrails(driver_cooldown_minutes=0))

        await place(database, service, make_request())
        second = await place(database, service, make_request())

        assert second.placed
        assert len(channel.sent) == 2


class TestQuotas:
    async def test_quota_par_mission(self, database):
        channel = RecordingChannel()
        service = DriverCallService(
            channel,
            guardrails=CallGuardrails(
                max_per_mission=2,
                max_per_driver_per_window=99,
                driver_cooldown_minutes=0,
            ),
        )

        outcomes = [await place(database, service, make_request()) for _ in range(3)]

        assert [outcome.placed for outcome in outcomes] == [True, True, False]
        assert outcomes[-1].blocked_reason is BlockedReason.MISSION_QUOTA
        assert len(channel.sent) == 2

    async def test_quota_par_chauffeur_sur_la_fenetre(self, database):
        """Deux missions du même chauffeur ne contournent pas son quota."""
        channel = RecordingChannel()
        service = DriverCallService(
            channel,
            guardrails=CallGuardrails(
                max_per_mission=99,
                max_per_driver_per_window=2,
                driver_window_minutes=60,
                driver_cooldown_minutes=0,
            ),
        )

        for index in range(3):
            outcome = await place(
                database,
                service,
                make_request(mission_id=f"msn_{index}", tms_now=TMS_NOW + timedelta(minutes=index)),
            )

        assert outcome.blocked_reason is BlockedReason.DRIVER_QUOTA
        assert len(channel.sent) == 2

    async def test_hors_fenetre_le_quota_se_libere(self, database):
        channel = RecordingChannel()
        service = DriverCallService(
            channel,
            guardrails=CallGuardrails(
                max_per_mission=99,
                max_per_driver_per_window=1,
                driver_window_minutes=60,
                driver_cooldown_minutes=0,
            ),
        )

        await place(database, service, make_request())
        later = await place(
            database,
            service,
            make_request(tms_now=TMS_NOW + timedelta(minutes=61)),
        )

        assert later.placed

    async def test_cooldown_entre_deux_appels(self, database):
        channel = RecordingChannel()
        service = DriverCallService(
            channel,
            guardrails=CallGuardrails(max_per_driver_per_window=9, driver_cooldown_minutes=15),
        )

        await place(database, service, make_request())
        trop_tot = await place(database, service, make_request(tms_now=TMS_NOW + timedelta(minutes=5)))
        assez_tard = await place(database, service, make_request(tms_now=TMS_NOW + timedelta(minutes=20)))

        assert trop_tot.blocked_reason is BlockedReason.DRIVER_COOLDOWN
        assert assez_tard.placed

    async def test_un_appel_bloque_ne_consomme_pas_de_quota(self, database):
        """Les index de quota sont partiels : un refus n'a dérangé personne."""
        channel = RecordingChannel()
        service = DriverCallService(
            channel,
            guardrails=CallGuardrails(max_per_mission=1, driver_cooldown_minutes=0),
        )

        await place(database, service, make_request(reason="trop court"))
        await place(database, service, make_request(severity=Severity.LOW))
        premier_vrai = await place(database, service, make_request())

        assert premier_vrai.placed
        assert len(await rows(database)) == 3


class TestRefusStatiques:
    async def test_motif_trop_court_refuse(self, database):
        channel = RecordingChannel()
        service = DriverCallService(channel)

        outcome = await place(database, service, make_request(reason="urgent"))

        assert outcome.blocked_reason is BlockedReason.REASON_TOO_SHORT
        assert not channel.sent
        assert str(MIN_REASON_LENGTH) in outcome.detail

    async def test_severite_insuffisante_refusee(self, database):
        """Un appel dérange : en deçà du seuil, une notification suffit."""
        service = DriverCallService(RecordingChannel(), guardrails=CallGuardrails())

        outcome = await place(database, service, make_request(severity=Severity.MEDIUM))

        assert outcome.blocked_reason is BlockedReason.SEVERITY_TOO_LOW

    async def test_sans_numero_refuse(self, database):
        service = DriverCallService(RecordingChannel())

        outcome = await place(database, service, make_request(phone=None))

        assert outcome.blocked_reason is BlockedReason.NO_PHONE
        stored = await rows(database)
        assert stored[0]["blocked_reason"] == BlockedReason.NO_PHONE.value


# --- L'outil exposé au modèle -------------------------------------------------


class StubMcp:
    """Serveur MCP réduit à l'instantané de mission."""

    def __init__(self, snapshot: Any | None) -> None:
        self._snapshot = snapshot
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, arguments: dict[str, Any]):
        from services.agents.integrations.mcp_client import ToolResult

        self.calls.append((name, arguments))
        payload = {"ok": True, "snapshot": to_jsonable(self._snapshot) if self._snapshot else None}
        return ToolResult(name=name, payload=payload)


class TestOutilDuModele:
    async def test_le_modele_ne_fournit_que_le_motif(self):
        """Le destinataire ne vient jamais du modèle."""
        parameters = DriverCallTool.definition()["function"]["parameters"]

        assert set(parameters["properties"]) == {"reason"}
        assert DriverCallTool.definition()["function"]["name"] == CALL_TOOL_NAME

    async def test_appel_resolu_depuis_la_mission(self, database):
        channel = RecordingChannel()
        snapshot = make_snapshot(deviation_km=8.0, deviation_minutes=25.0)
        mcp = StubMcp(snapshot)
        tool = DriverCallTool(DriverCallService(channel), database, mcp)  # type: ignore[arg-type]
        alert = await seed_alert(database)
        investigation_id = await seed_investigation(database, alert)

        payload = await tool.invoke(make_investigation_request(alert, investigation_id), {"reason": REASON})

        assert payload["placed"] is True
        # Le numéro et l'heure viennent du TMS, pas du modèle.
        assert channel.sent[0].address == snapshot.driver.phone
        stored = await rows(database)
        assert stored[0]["driver_id"] == DRIVER_ID
        assert stored[0]["requested_at"] == snapshot.observed_at
        assert stored[0]["alert_id"] == alert.id
        # Aucune déclaration inventée : le canal console ne rapporte rien.
        assert payload["driver_statement"] is None

    async def test_refus_rendu_comme_resultat_et_non_comme_erreur(self, database):
        """Un quota atteint n'est pas une panne : le modèle doit poursuivre."""
        snapshot = make_snapshot()
        tool = DriverCallTool(DriverCallService(RecordingChannel()), database, StubMcp(snapshot))  # type: ignore[arg-type]
        alert = await seed_alert(database)
        request = make_investigation_request(alert, await seed_investigation(database, alert))

        await tool.invoke(request, {"reason": REASON})
        replay = await tool.invoke(request, {"reason": REASON})

        assert replay["ok"] is True
        assert replay["placed"] is False
        assert replay["blocked_reason"] == BlockedReason.ALREADY_CALLED.value

    async def test_mission_illisible_pas_dappel(self, database):
        tool = DriverCallTool(DriverCallService(RecordingChannel()), database, StubMcp(None))  # type: ignore[arg-type]
        alert = await seed_alert(database)
        investigation_id = await seed_investigation(database, alert)

        payload = await tool.invoke(make_investigation_request(alert, investigation_id), {"reason": REASON})

        assert payload["placed"] is False
        assert payload["blocked_reason"] == BlockedReason.NO_PHONE.value
        assert not await rows(database)

    async def test_argument_invalide_rendu_au_modele(self, database):
        tool = DriverCallTool(DriverCallService(RecordingChannel()), database, StubMcp(make_snapshot()))  # type: ignore[arg-type]
        alert = await seed_alert(database)

        payload = await tool.invoke(
            make_investigation_request(alert, await seed_investigation(database, alert)),
            {"motif": REASON},
        )

        assert payload["ok"] is False
        assert not await rows(database)


class TestRobustesseDesGardeFous:
    async def test_un_refus_temporaire_ne_devient_pas_definitif(self, database):
        """Un cooldown écoulé rend l'appel possible, malgré le refus tracé.

        C'est le piège qu'une unicité totale sur la clé d'idempotence tendrait :
        le refus écrit une ligne portant la clé de l'alerte, et la tentative
        suivante — légitime, le cooldown étant passé — se verrait refusée pour
        « déjà appelé ». Le chauffeur ne serait jamais joint.
        """
        channel = RecordingChannel()
        service = DriverCallService(channel, guardrails=CallGuardrails(driver_cooldown_minutes=15))
        premiere = await seed_alert(database, mission_id="msn_1")
        seconde = await seed_alert(database, mission_id="msn_2")

        # Le chauffeur est appelé pour sa première mission…
        await place(database, service, make_request(alert_id=premiere.id, mission_id="msn_1"))
        # … sa seconde mission dérive dans les minutes qui suivent : refusé.
        bloque = await place(
            database,
            service,
            make_request(
                alert_id=seconde.id,
                mission_id="msn_2",
                tms_now=TMS_NOW + timedelta(minutes=2),
            ),
        )
        assert bloque.blocked_reason is BlockedReason.DRIVER_COOLDOWN

        # Une demi-heure de temps TMS plus tard, la même alerte peut aboutir.
        apres = await place(
            database,
            service,
            make_request(
                alert_id=seconde.id,
                mission_id="msn_2",
                tms_now=TMS_NOW + timedelta(minutes=30),
            ),
        )

        assert apres.placed
        assert len(channel.sent) == 2

    async def test_plan_generique_de_postgres(self, database):
        """Vingt appels par la même instruction préparée.

        Au-delà de cinq exécutions, PostgreSQL bascule sur un plan générique :
        un prédicat paramétré cesse alors de correspondre à l'index partiel de
        la clé d'idempotence, et l'insertion échouerait sur l'unicité au lieu
        de ne rien faire. Ce test passerait avec onze appels et casserait au
        douzième si le prédicat redevenait paramétré.
        """
        service = DriverCallService(
            RecordingChannel(),
            guardrails=CallGuardrails(
                max_per_mission=99,
                max_per_driver_per_window=99,
                driver_cooldown_minutes=0,
            ),
        )

        for index in range(20):
            outcome = await place(
                database,
                service,
                make_request(mission_id=f"msn_{index}", tms_now=TMS_NOW + timedelta(minutes=index)),
            )
            assert outcome.placed, f"appel {index} refusé : {outcome.detail}"

        assert len(await rows(database)) == 20
