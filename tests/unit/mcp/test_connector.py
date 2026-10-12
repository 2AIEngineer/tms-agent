"""Tests du connecteur HTTP vers le TMS.

Ce qui est vérifié : la lecture seule, la traduction des échecs en codes du
contrat pivot, et le fait qu'on ne s'acharne que sur ce qui mérite une reprise.
"""

import httpx
import pytest

from services.mcp_server.connectors.base import TmsFailure
from services.mcp_server.connectors.rest_api_tms import RestApiTmsConnector
from shared.mapped_dto import ToolErrorCode
from tests.fake_tms import TMS_NOW, FakeTms, mission_payload, page


def build(tms: FakeTms, **kwargs) -> RestApiTmsConnector:
    return RestApiTmsConnector(
        "http://tms.test/v1",
        client=tms.client(),
        retry_backoff_s=0.001,  # les tests n'attendent pas réellement
        **kwargs,
    )


class TestLectures:
    async def test_heure_du_tms(self):
        """F3 : l'heure de référence vient du TMS, lue sur /health."""
        tms = FakeTms()
        assert await build(tms).tms_time() == TMS_NOW

    async def test_missions_filtrees_par_statut(self):
        tms = FakeTms(
            missions=[
                mission_payload("msn_1", status="in_progress"),
                mission_payload("msn_2", status="completed"),
                mission_payload("msn_3", status="planned"),
            ]
        )
        missions = await build(tms).list_missions(statuses=["planned", "in_progress"])
        assert {mission["id"] for mission in missions} == {"msn_1", "msn_3"}

    async def test_detail_de_mission(self):
        mission = await build(FakeTms()).get_mission("msn_1")
        assert mission["reference"] == "REF-0001"
        assert len(mission["events"]) == 3

    async def test_ressource_absente_rend_none(self):
        """Un itinéraire non planifié est un fait, pas une erreur."""
        tms = FakeTms(route=None)
        assert await build(tms).get_route("msn_1") is None

    async def test_destinataires_par_role(self):
        users = await build(FakeTms()).list_users_by_role("ops_supervisor")
        assert [user["id"] for user in users] == ["usr_20"]

    async def test_role_sans_utilisateur_rend_liste_vide(self):
        users = await build(FakeTms()).list_users_by_role("inexistant")
        assert users == []


class TestLectureSeule:
    async def test_seul_le_verbe_get_est_employe(self):
        """Contrainte 1 : le système ne modifie jamais le TMS.

        Vérifié sur le trafic réellement émis, et non sur une revue de code :
        c'est la seule façon de garantir qu'aucun chemin ne glisse un POST.
        """
        tms = FakeTms()
        connector = build(tms)

        await connector.tms_time()
        await connector.list_missions()
        await connector.get_mission("msn_1")
        await connector.get_deviation("msn_1")
        await connector.get_eta("msn_1")
        await connector.get_vehicle("veh_1")
        await connector.get_driver("drv_1")
        await connector.list_users_by_role("ops_agent")

        assert set(tms.methods) == {"GET"}


class TestTraductionDesEchecs:
    @pytest.mark.parametrize(
        ("status", "expected", "retryable"),
        [
            (404, ToolErrorCode.MISSION_NOT_FOUND, False),
            (422, ToolErrorCode.INVALID_ARGUMENT, False),
            (403, ToolErrorCode.NOT_PERMITTED, False),
            (429, ToolErrorCode.TMS_RATE_LIMIT, True),
            (500, ToolErrorCode.TMS_UNAVAILABLE, True),
            (503, ToolErrorCode.TMS_UNAVAILABLE, True),
        ],
    )
    async def test_statut_http_vers_code_pivot(self, status, expected, retryable):
        """La distinction 404 / 5xx est la plus importante de la frontière.

        Un LLM qui les confondrait conclurait « la mission n'existe pas » devant
        un TMS simplement en panne.
        """
        tms = FakeTms()
        tms.fail("/missions/msn_1", status)

        with pytest.raises(TmsFailure) as caught:
            await build(tms, max_retries=0).get_mission("msn_1")

        assert caught.value.error.code is expected
        assert caught.value.retryable is retryable

    async def test_delai_depasse(self):
        tms = FakeTms()
        tms.timeout("/missions/msn_1")

        with pytest.raises(TmsFailure) as caught:
            await build(tms, max_retries=0).get_mission("msn_1")

        assert caught.value.error.code is ToolErrorCode.TMS_TIMEOUT
        assert caught.value.retryable is True

    async def test_tms_injoignable(self):
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connexion refusée", request=request)

        connector = RestApiTmsConnector(
            "http://tms.test/v1",
            client=httpx.AsyncClient(base_url="http://tms.test/v1", transport=httpx.MockTransport(refuse)),
            max_retries=0,
        )
        with pytest.raises(TmsFailure) as caught:
            await connector.tms_time()
        assert caught.value.error.code is ToolErrorCode.TMS_UNAVAILABLE

    async def test_message_du_tms_remonte(self):
        """Le détail RFC 7807 du TMS est conservé : il évite d'ouvrir les journaux."""
        tms = FakeTms()
        tms.fail("/missions/msn_9", 404)

        with pytest.raises(TmsFailure) as caught:
            await build(tms, max_retries=0).get_mission("msn_9")

        assert "msn_9" in caught.value.error.message

    async def test_reponse_illisible(self):
        def garbage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>pas du json</html>")

        connector = RestApiTmsConnector(
            "http://tms.test/v1",
            client=httpx.AsyncClient(base_url="http://tms.test/v1", transport=httpx.MockTransport(garbage)),
            max_retries=0,
        )
        with pytest.raises(TmsFailure) as caught:
            await connector.tms_time()
        assert caught.value.error.code is ToolErrorCode.TMS_UNAVAILABLE


class TestReprises:
    async def test_erreur_transitoire_reessayee_et_resolue(self):
        """Un 503 isolé ne doit pas faire échouer un cycle."""
        tms = FakeTms()
        tms.fail("/missions/msn_1", 503, times=1)

        mission = await build(tms, max_retries=2).get_mission("msn_1")

        assert mission["id"] == "msn_1"
        assert tms.calls["/missions/msn_1"] == 2

    async def test_erreur_definitive_non_reessayee(self):
        """S'acharner sur un 404 ne ferait que consommer le quota du TMS."""
        tms = FakeTms()
        tms.fail("/missions/msn_1", 404)

        with pytest.raises(TmsFailure):
            await build(tms, max_retries=3).get_mission("msn_1")

        assert tms.calls["/missions/msn_1"] == 1

    async def test_abandon_apres_le_plafond(self):
        tms = FakeTms()
        tms.timeout("/health")

        with pytest.raises(TmsFailure):
            await build(tms, max_retries=2).tms_time()

        assert tms.calls["/health"] == 3  # tentative initiale + 2 reprises


class TestPagination:
    async def test_curseur_suivi_jusquau_bout(self):
        """Le TMS pagine : une mission oubliée en page 2 ne serait pas surveillée."""
        pages = iter(
            [
                page([mission_payload("msn_1")], cursor="c1", has_more=True),
                page([mission_payload("msn_2")], cursor="c2", has_more=True),
                page([mission_payload("msn_3")], has_more=False),
            ]
        )

        def paginated(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(pages))

        connector = RestApiTmsConnector(
            "http://tms.test/v1",
            client=httpx.AsyncClient(base_url="http://tms.test/v1", transport=httpx.MockTransport(paginated)),
        )
        missions = await connector.list_missions()
        assert [mission["id"] for mission in missions] == ["msn_1", "msn_2", "msn_3"]

    async def test_pagination_sans_fin_interrompue(self):
        """Un TMS qui annonce toujours une page suivante ne doit pas bloquer le cycle."""

        def endless(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=page([mission_payload("msn_1")], cursor="c", has_more=True))

        connector = RestApiTmsConnector(
            "http://tms.test/v1",
            client=httpx.AsyncClient(base_url="http://tms.test/v1", transport=httpx.MockTransport(endless)),
        )
        missions = await connector.list_missions()
        assert len(missions) == 50  # plafond MAX_PAGES
