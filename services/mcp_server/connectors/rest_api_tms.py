"""Connecteur du TMS de référence.

Ne contient plus aucun chemin d'endpoint ni aucun nom de champ : tout le
dialecte de ce TMS est décrit dans `dialects/x_tms.yaml`. Cette classe n'est
qu'un `HttpTmsConnector` préchargé avec ce dialecte.

Elle subsiste sous ce nom parce que c'est le connecteur par défaut du POC et
que le reste du système le nomme ainsi. Brancher un autre TMS ne consiste pas à
écrire une classe analogue, mais à écrire son dialecte puis à l'ouvrir avec
`connector_for("son_tms", base_url)`.
"""

from typing import Any

import httpx

from ..guardrails import TmsGuardrails
from .dialect import TmsDialect, load_named_dialect
from .http_tms import HttpTmsConnector
from .profile import TmsProfile

DEFAULT_DIALECT = "x_tms"


class RestApiTmsConnector(HttpTmsConnector):
    """TMS de référence : contrat REST `/v1`, pagination par curseur."""

    def __init__(
        self,
        base_url: str,
        *,
        dialect: TmsDialect | None = None,
        api_key: str | None = None,
        timeout_s: float = 5.0,
        connect_timeout_s: float = 2.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        guardrails: TmsGuardrails | None = None,
        client: httpx.AsyncClient | None = None,
        profile: TmsProfile | None = None,
    ) -> None:
        super().__init__(
            base_url,
            dialect or load_named_dialect(DEFAULT_DIALECT),
            api_key=api_key,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            max_retries=max_retries,
            retry_backoff_s=retry_backoff_s,
            guardrails=guardrails,
            client=client,
            profile=profile,
        )


def connector_for(dialect_name: str, base_url: str, **kwargs: Any) -> HttpTmsConnector:
    """Ouvre une connexion vers un TMS, à partir du nom de son dialecte.

    C'est le point d'entrée pour brancher un TMS : un nom de dialecte et une
    racine d'API suffisent, sans classe à écrire.
    """
    return HttpTmsConnector(base_url, load_named_dialect(dialect_name), **kwargs)
