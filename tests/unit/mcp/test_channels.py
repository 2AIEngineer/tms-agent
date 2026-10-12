"""Tests des canaux d'envoi.

Le canal de développement est le comportement par défaut : aucun fournisseur
n'est contractualisé, et le POC tourne sur des numéros fictifs. Ce qui est
vérifié ici, c'est le contrat que devra respecter un adaptateur réel.
"""

from datetime import UTC, datetime

import pytest

from shared.channels import ConsoleChannel, Message, SendResult, console_registry
from shared.channels.console import BLOCK_WIDTH, render_block
from shared.mapped_dto import NotificationChannel, Severity

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


def message(channel: NotificationChannel = NotificationChannel.SMS) -> Message:
    return Message(
        channel=channel,
        address="+33600000000",
        title="Dérive d'itinéraire",
        body="Le véhicule AB-123-CD s'est écarté de 7.2 km.",
        severity=Severity.HIGH,
        alert_id="a1",
        mission_id="msn_1",
        recipient_id="usr_20",
    )


class TestConsoleChannel:
    async def test_message_memorise(self):
        """Mémoriser permet de vérifier *ce qui serait parti*, pas juste un statut."""
        channel = ConsoleChannel(NotificationChannel.SMS)
        result = await channel.send(message(), sent_at=NOW)

        assert result.delivered is True
        assert result.provider == "console"
        assert result.reference
        assert channel.sent[0].body.startswith("Le véhicule")

    async def test_panne_de_fournisseur_simulable(self):
        channel = ConsoleChannel(NotificationChannel.SMS)
        channel.fail_with = SendResult.transient_failure("console", "quota atteint")

        result = await channel.send(message(), sent_at=NOW)

        assert result.delivered is False
        assert result.retryable is True
        assert channel.sent == []

    async def test_registre_couvre_les_quatre_canaux(self):
        registry = console_registry()
        assert set(registry) == set(NotificationChannel)
        for channel_type, channel in registry.items():
            assert channel.channel is channel_type


class TestContratDeResultat:
    def test_succes(self):
        result = SendResult.ok("twilio", reference="SM123")
        assert (result.delivered, result.retryable, result.reference) == (True, False, "SM123")

    def test_echec_definitif_non_retryable(self):
        """Un numéro invalide ne deviendra pas valide en réessayant."""
        result = SendResult.permanent_failure("twilio", "numéro invalide")
        assert result.delivered is False
        assert result.retryable is False

    def test_echec_passager_retryable(self):
        result = SendResult.transient_failure("twilio", "service indisponible")
        assert result.retryable is True

    @pytest.mark.parametrize("channel", list(NotificationChannel))
    async def test_tous_les_canaux_acheminent(self, channel):
        result = await ConsoleChannel(channel).send(message(channel), sent_at=NOW)
        assert result.delivered is True


class TestAffichageConsole:
    """Le journal du worker est la seule fenêtre sur ce que le système envoie.

    Ce qui est vérifié n'est pas l'esthétique mais la présence des informations
    qu'un superviseur cherche : le canal, la gravité, qui a été joint, à quelle
    adresse, pour quelle mission.
    """

    def test_bloc_contient_les_informations_dexploitation(self):
        block = render_block(
            Message(
                channel=NotificationChannel.SMS,
                address="+212600000004",
                title="Dérive d'itinéraire",
                body="Le véhicule AB-123-CD s'est écarté de 7.96 km.",
                severity=Severity.HIGH,
                alert_id="f48b73ce",
                mission_id="msn_1",
                recipient_id="usr_sup_04",
                metadata={"role": "ops_supervisor", "management_level": "M+2"},
            ),
            sent_at=NOW,
            status="ENVOYÉE",
            reference="console-abc",
        )

        for expected in (
            "SMS",
            "HIGH",
            "ENVOYÉE",
            "ops_supervisor",
            "usr_sup_04",
            "M+2",
            "+212600000004",
            "msn_1",
            "f48b73ce",
            "Dérive d'itinéraire",
            "console-abc",
        ):
            assert expected in block, f"information absente du bloc : {expected}"

    def test_lignes_alignees_sur_une_largeur_constante(self):
        """Un bloc mal fermé est illisible dans un flux de journal."""
        block = render_block(message(), sent_at=NOW, status="ENVOYÉE")
        lines = block.strip().splitlines()

        assert lines[0].startswith("┌─")
        assert lines[-1].startswith("└")
        assert len(lines[0]) == len(lines[-1])
        assert all(line.startswith("│") for line in lines[1:-1])

    def test_message_long_replie_sans_casser_lalignement(self):
        long_body = "Le véhicule " + "très " * 40 + "en retard."
        block = render_block(
            Message(
                channel=NotificationChannel.SMS,
                address="+33600000000",
                title="t",
                body=long_body,
                severity=Severity.LOW,
                alert_id="a",
                mission_id="m",
                recipient_id="r",
            ),
            sent_at=NOW,
            status="ENVOYÉE",
        )
        corps = [line for line in block.splitlines() if line.startswith("│")]

        assert len(corps) > 7  # le message occupe plusieurs lignes
        assert all(len(line) <= BLOCK_WIDTH for line in corps)

    def test_absence_dadresse_distingue_in_app_et_donnee_manquante(self):
        """Un SMS sans numéro est une anomalie ; l'in-app n'a légitimement pas d'adresse."""
        in_app = render_block(
            Message(
                channel=NotificationChannel.IN_APP,
                address=None,
                title="t",
                body="b",
                severity=Severity.LOW,
                alert_id="a",
                mission_id="m",
                recipient_id="r",
            ),
            sent_at=NOW,
            status="ENVOYÉE",
        )
        sms = render_block(
            Message(
                channel=NotificationChannel.SMS,
                address=None,
                title="t",
                body="b",
                severity=Severity.LOW,
                alert_id="a",
                mission_id="m",
                recipient_id="r",
            ),
            sent_at=NOW,
            status="NON ENVOYÉE",
        )

        assert "remise dans l'application" in in_app
        assert "aucune adresse dans le TMS" in sms

    def test_echec_definitif_indique_la_reprise_humaine(self):
        block = render_block(
            message(),
            sent_at=NOW,
            status="NON ENVOYÉE",
            failure_reason="numéro invalide",
            retryable=False,
        )
        assert "numéro invalide" in block
        assert "reprise humaine" in block

    def test_echec_passager_indique_la_reprise(self):
        block = render_block(
            message(),
            sent_at=NOW,
            status="NON ENVOYÉE",
            failure_reason="fournisseur indisponible",
            retryable=True,
        )
        assert "à réessayer" in block

    async def test_un_seul_appel_au_journal_par_envoi(self, caplog):
        """Deux envois simultanés entrelaceraient sinon leurs blocs."""
        import logging

        channel = ConsoleChannel(NotificationChannel.SMS)
        with caplog.at_level(logging.INFO, logger="shared.channels.console"):
            await channel.send(message(), sent_at=NOW)

        assert len(caplog.records) == 1
        assert "┌─" in caplog.records[0].getMessage()
