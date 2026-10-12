"""Tests des contrats pivot.

On teste ici les propriétés dont dépendent les autres services : l'ordre des
sévérités, la distinction entre erreur technique et information métier, et la
rigidité volontaire des DTO.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shared.mapped_dto import (
    Alert,
    MissionEvent,
    NotificationChannel,
    NotificationRequest,
    Severity,
    ToolError,
    ToolErrorCode,
)
from shared.mapped_dto.events import EventType

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class TestSeverity:
    def test_ordre_croissant(self):
        assert Severity.LOW < Severity.MEDIUM < Severity.HIGH < Severity.CRITICAL

    def test_tri_decroissant(self):
        """F9 et F19 traitent les alertes les plus graves d'abord."""
        melange = [Severity.MEDIUM, Severity.CRITICAL, Severity.LOW, Severity.HIGH]
        assert sorted(melange, reverse=True) == [
            Severity.CRITICAL,
            Severity.HIGH,
            Severity.MEDIUM,
            Severity.LOW,
        ]

    def test_ordre_non_alphabetique(self):
        """Un tri de chaînes donnerait critical < high < low < medium, soit l'inverse du besoin."""
        assert sorted(["critical", "high", "low", "medium"]) != [
            s.value for s in sorted(Severity, reverse=True)
        ]


class TestToolError:
    @pytest.mark.parametrize(
        "code",
        [ToolErrorCode.TMS_TIMEOUT, ToolErrorCode.TMS_RATE_LIMIT, ToolErrorCode.TMS_UNAVAILABLE],
    )
    def test_incidents_techniques_retryables(self, code):
        assert ToolError.of(code, "indisponible").retryable is True

    @pytest.mark.parametrize(
        "code",
        [ToolErrorCode.MISSION_NOT_FOUND, ToolErrorCode.INVALID_ARGUMENT, ToolErrorCode.NOT_PERMITTED],
    )
    def test_informations_metier_non_retryables(self, code):
        """Retenter une mission inexistante ferait boucler l'agent indéfiniment."""
        assert ToolError.of(code, "absente").retryable is False


class TestRigiditeDesContrats:
    def test_champ_inattendu_refuse(self):
        """Un mapper qui invente un champ doit échouer tôt, pas produire un trou silencieux."""
        with pytest.raises(ValidationError):
            MissionEvent(
                id="E1",
                mission_id="M1",
                type=EventType.DRIVER_MESSAGE,
                occurred_at=NOW,
                champ_invente="oups",
            )

    def test_evenement_immuable(self):
        """Une règle ne doit jamais pouvoir modifier ce qu'elle observe."""
        event = MissionEvent(id="E1", mission_id="M1", type=EventType.DRIVER_MESSAGE, occurred_at=NOW)
        with pytest.raises(ValidationError):
            event.id = "E2"

    def test_type_tms_inconnu_conserve_dans_raw_type(self):
        """Un type propre à un TMS n'est pas perdu : il est rangé sous `other`."""
        event = MissionEvent(
            id="E1",
            mission_id="M1",
            type=EventType.OTHER,
            raw_type="CUSTOM_BORDER_CROSSING",
            occurred_at=NOW,
        )
        assert event.type is EventType.OTHER
        assert event.raw_type == "CUSTOM_BORDER_CROSSING"

    @pytest.mark.parametrize("level", ["M0", "niveau 1", "M+", ""])
    def test_niveau_de_management_mal_forme_refuse(self, level):
        with pytest.raises(ValidationError):
            NotificationRequest(role="ops_agent", management_level=level, channel=NotificationChannel.SMS)

    def test_provenance_obligatoire_sur_une_alerte(self):
        """Sans provenance, deux TMS verraient leurs alertes se confondre.

        Le champ est obligatoire et non optionnel avec un repli : un oubli doit
        échouer à la construction, et non produire une alerte attribuée au
        hasard dont la déduplication écraserait celle d'un autre TMS.
        """
        with pytest.raises(ValidationError):
            Alert(
                mission_id="MSN-1",
                rule_id="deviation_persistent",
                rule_version=2,
                severity=Severity.HIGH,
                category="route_deviation",
                title="Dérive",
                message="…",
                deduplication_key="deviation:MSN-1",
                detected_at=NOW,
            )

    def test_alerte_conserve_la_version_de_regle(self):
        """Sans la version, une alerte devient inexplicable dès que son seuil change."""
        alert = Alert(
            tms_id="x_tms",
            mission_id="MSN-1",
            rule_id="deviation_persistent",
            rule_version=2,
            severity=Severity.HIGH,
            category="route_deviation",
            title="Dérive",
            message="…",
            deduplication_key="deviation:MSN-1",
            detected_at=NOW,
        )
        assert alert.rule_version == 2
