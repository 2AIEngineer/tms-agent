"""Tests de la résolution des destinataires de notification (F16)."""

import pytest

from services.mcp_server.connectors.base import TmsFailure
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from services.mcp_server.recipients import RecipientResolver
from shared.mapped_dto import NotificationChannel
from tests.fake_tms import FakeTms, mission_payload


def resolver(tms: FakeTms) -> RecipientResolver:
    return RecipientResolver(
        RestApiTmsConnector("http://tms.test/v1", client=tms.client(), retry_backoff_s=0.001)
    )


class TestResolutionParRole:
    async def test_superviseur_par_sms(self):
        recipients = await resolver(FakeTms()).resolve_role("ops_supervisor", NotificationChannel.SMS)
        assert len(recipients) == 1
        assert recipients[0].user_id == "usr_20"
        assert recipients[0].management_level == "M+2"
        assert recipients[0].address == "+33600000000"
        assert recipients[0].reachable is True

    async def test_adresse_depend_du_canal(self):
        """Un appel veut un numéro, un e-mail une adresse : ce n'est pas le même champ."""
        sms = await resolver(FakeTms()).resolve_role("ops_manager", NotificationChannel.SMS)
        email = await resolver(FakeTms()).resolve_role("ops_manager", NotificationChannel.EMAIL)

        assert sms[0].address == "+33600000000"
        assert email[0].address == "usr_30@example.com"

    async def test_in_app_ne_demande_aucune_adresse(self):
        """L'identifiant utilisateur suffit : rien à résoudre côté annuaire."""
        recipients = await resolver(FakeTms()).resolve_role("ops_agent", NotificationChannel.IN_APP)
        assert recipients[0].address is None
        assert recipients[0].reachable is True

    async def test_role_sans_titulaire(self):
        """Signalé, mais sans faire échouer : une règle peut viser un rôle vacant."""
        recipients = await resolver(FakeTms()).resolve_role("astreinte", NotificationChannel.SMS)
        assert recipients == []

    async def test_annuaire_injoignable_remonte(self):
        """Distinct d'un rôle vacant : ici l'envoi devra être réessayé."""
        tms = FakeTms()
        tms.fail("/roles/ops_agent/users", 503)

        with pytest.raises(TmsFailure) as caught:
            await resolver(tms).resolve_role("ops_agent", NotificationChannel.IN_APP)
        assert caught.value.retryable is True


class TestChauffeurDeLaMission:
    async def test_le_role_driver_designe_le_chauffeur_de_cette_mission(self):
        """Le traiter comme les autres rôles appellerait toute la flotte à chaque dérive."""
        recipients = await resolver(FakeTms()).resolve_role(
            "driver", NotificationChannel.CALL, mission=mission_payload()
        )
        assert len(recipients) == 1
        assert recipients[0].user_id == "drv_1"
        assert recipients[0].address == "+33600000001"

    async def test_sans_mission_aucun_destinataire(self):
        recipients = await resolver(FakeTms()).resolve_role("driver", NotificationChannel.CALL)
        assert recipients == []

    async def test_chauffeur_introuvable(self):
        tms = FakeTms(driver=None)
        recipients = await resolver(tms).resolve_role(
            "driver", NotificationChannel.CALL, mission=mission_payload()
        )
        assert recipients == []


class TestJoignabilite:
    async def test_destinataire_sans_numero_signale(self):
        """F17 : un envoi impossible est marqué en échec, pas réessayé sans fin.

        C'est une correction à faire dans le TMS, pas un incident passager.
        """
        tms = FakeTms(
            users_by_role={
                "ops_supervisor": [
                    {
                        "id": "usr_20",
                        "name": "Marc Dubois",
                        "role": "ops_supervisor",
                        "management_level": "M+2",
                        "phone": None,
                        "email": "marc@example.com",
                    }
                ]
            }
        )
        recipients = await resolver(tms).resolve_role("ops_supervisor", NotificationChannel.SMS)

        assert recipients[0].reachable is False
        assert "phone" in recipients[0].unreachable_reason
        assert "usr_20" in recipients[0].unreachable_reason

    async def test_meme_destinataire_joignable_par_un_autre_canal(self):
        tms = FakeTms(
            users_by_role={
                "ops_supervisor": [
                    {
                        "id": "usr_20",
                        "name": "Marc Dubois",
                        "role": "ops_supervisor",
                        "management_level": "M+2",
                        "phone": None,
                        "email": "marc@example.com",
                    }
                ]
            }
        )
        recipients = await resolver(tms).resolve_role("ops_supervisor", NotificationChannel.EMAIL)
        assert recipients[0].reachable is True


class TestDesignationNominative:
    async def test_utilisateur_par_identifiant(self):
        recipient = await resolver(FakeTms()).resolve_user("usr_30", NotificationChannel.EMAIL)
        assert recipient is not None
        assert recipient.name == "Ines Rossi"

    async def test_utilisateur_inconnu(self):
        assert await resolver(FakeTms()).resolve_user("usr_999", NotificationChannel.SMS) is None
