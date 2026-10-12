"""Connecteur HTTP générique, piloté par un dialecte.

Ses méthodes **ne changent pas d'un TMS à l'autre** : `list_missions`,
`get_route`, `get_deviation`… C'est le contrat que le reste du système connaît.
Ce qui change est le dialecte : à quel chemin, avec quels paramètres, dans
quelle enveloppe, selon quel style de pagination.

Aucun nom d'endpoint n'apparaît donc dans ce fichier. Brancher un TMS consiste
à écrire son dialecte ; ce code, lui, ne bouge plus.

Lecture seule : `_get` est la seule méthode de transport, et le seul verbe HTTP
employé. Ce n'est pas une convention à respecter mais une impossibilité
structurelle d'écrire dans le TMS.
"""

import asyncio
import logging
import os
from base64 import b64encode
from datetime import datetime
from typing import Any

import httpx

from shared.mapped_dto import ToolErrorCode
from shared.utils.serialization import parse_datetime, to_jsonable

from ..guardrails import TmsGuardrails
from .base import TmsFailure, failure_from_status
from .dialect import AuthStyle, Entity, Operation, PaginationStyle, TmsDialect
from .paths import extract
from .profile import TmsProfile, build_profile

logger = logging.getLogger(__name__)

# Statuts qui méritent une seconde tentative : le TMS est momentanément
# indisponible ou nous freine. Un 404 ou un 400 ne changeront pas d'avis.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpTmsConnector:
    """Client HTTP d'un TMS, dont tout le dialecte est déclaré en configuration."""

    def __init__(
        self,
        base_url: str,
        dialect: TmsDialect,
        *,
        api_key: str | None = None,
        timeout_s: float = 5.0,
        connect_timeout_s: float = 2.0,
        max_retries: int = 2,
        retry_backoff_s: float = 0.25,
        guardrails: TmsGuardrails | None = None,
        client: httpx.AsyncClient | None = None,
        profile: TmsProfile | None = None,
    ) -> None:
        self._dialect = dialect
        self._profile = profile or build_profile(dialect)
        self._base_url = (dialect.api_root or base_url).rstrip("/")
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s
        self._guardrails = guardrails

        headers = {"Accept": "application/json"}
        headers.update(self._auth_headers(api_key))
        headers.update(self._profile.extra_headers())

        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
        )
        self._owns_client = client is None
        # Les en-têtes sont appliqués au client **dans tous les cas**, y compris
        # lorsqu'il est fourni de l'extérieur. Les passer au constructeur seul
        # ferait perdre l'authentification à tout appelant qui injecte son
        # propre client — silencieusement, et seulement en production.
        self._client.headers.update(headers)

    @property
    def dialect(self) -> TmsDialect:
        return self._dialect

    @property
    def tms_id(self) -> str:
        return self._dialect.tms_id

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- Authentification -----------------------------------------------------

    def _auth_headers(self, api_key: str | None) -> dict[str, str]:
        """En-têtes d'authentification décrits par le dialecte.

        Le secret vient d'une variable d'environnement, jamais du dialecte
        lui-même : un dialecte est un fichier versionné.
        """
        auth = self._dialect.auth
        if auth.style is AuthStyle.NONE:
            return {}

        secret = api_key or (os.environ.get(auth.secret_env) if auth.secret_env else None)
        if not secret:
            logger.warning(
                "Dialecte %s : authentification '%s' demandée mais %s est vide",
                self._dialect.tms_id,
                auth.style.value,
                auth.secret_env,
            )
            return {}

        if auth.style is AuthStyle.BASIC:
            secret = b64encode(secret.encode()).decode()

        value = f"{auth.prefix} {secret}".strip() if auth.prefix else secret
        return {auth.header: value}

    # --- Transport ------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Lecture GET, avec garde-fous, reprises et échecs qualifiés."""
        last_failure: TmsFailure | None = None

        for attempt in range(self._max_retries + 1):
            try:
                if self._guardrails is not None:
                    async with self._guardrails.slot():
                        response = await self._client.get(path, params=self._clean(params))
                else:
                    response = await self._client.get(path, params=self._clean(params))
            except httpx.TimeoutException:
                last_failure = TmsFailure.of(
                    ToolErrorCode.TMS_TIMEOUT,
                    f"Délai dépassé en lisant {path}.",
                    {"resource": path, "attempt": attempt + 1},
                )
            except httpx.HTTPError as error:
                last_failure = TmsFailure.of(
                    ToolErrorCode.TMS_UNAVAILABLE,
                    f"TMS injoignable en lisant {path} — {error}.",
                    {"resource": path, "attempt": attempt + 1},
                )
            else:
                if response.status_code < 300:
                    return self._decode(response, path)
                if response.status_code not in RETRYABLE_STATUSES:
                    raise failure_from_status(response.status_code, path, self._problem_detail(response))
                last_failure = failure_from_status(response.status_code, path, self._problem_detail(response))

            if attempt < self._max_retries:
                # Progression exponentielle : si le TMS est saturé, le marteler
                # au même rythme ne fait qu'aggraver sa situation.
                await asyncio.sleep(self._retry_backoff_s * (2**attempt))

        assert last_failure is not None
        logger.warning("Lecture TMS abandonnée après %d tentative(s) : %s", self._max_retries + 1, path)
        raise last_failure

    @staticmethod
    def _clean(params: dict[str, Any] | None) -> dict[str, Any] | None:
        if not params:
            return None
        cleaned = {key: value for key, value in params.items() if value is not None}
        return {
            key: to_jsonable(value) if isinstance(value, datetime) else value
            for key, value in cleaned.items()
        } or None

    @staticmethod
    def _decode(response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as error:
            raise TmsFailure.of(
                ToolErrorCode.TMS_UNAVAILABLE,
                f"Réponse illisible du TMS sur {path} : {error}.",
                {"resource": path},
            ) from error

    @staticmethod
    def _problem_detail(response: httpx.Response) -> str | None:
        """Extrait le détail d'une erreur RFC 7807, si le TMS en fournit une."""
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        detail = body.get("detail") or body.get("title")
        return str(detail) if detail else None

    # --- Résolution d'une opération du dialecte -------------------------------

    def supports(self, operation: Operation) -> bool:
        return self._dialect.supports(operation)

    def _resolve(
        self,
        operation: Operation,
        *,
        path_args: dict[str, Any] | None = None,
        param_args: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Construit le chemin et les paramètres de cette opération pour ce TMS."""
        spec = self._dialect.operation(operation)
        assert spec.path is not None  # garanti par la validation du dialecte

        path = spec.path.format(**(path_args or {}))

        # Les gabarits `{nom}` des paramètres déclarés sont remplis par les
        # arguments de l'appel ; un argument absent retire le paramètre, ce qui
        # évite d'envoyer `since=None` au TMS.
        params: dict[str, Any] = {}
        values = param_args or {}
        for key, template in spec.params.items():
            if template.startswith("{") and template.endswith("}"):
                name = template[1:-1]
                if name in values and values[name] is not None:
                    params[key] = values[name]
            else:
                params[key] = template
        return path, params

    async def _call(
        self,
        operation: Operation,
        *,
        path_args: dict[str, Any] | None = None,
        param_args: dict[str, Any] | None = None,
    ) -> Any:
        """Appelle une opération et rend sa charge utile désenveloppée."""
        path, params = self._resolve(operation, path_args=path_args, param_args=param_args)
        payload = await self._get(path, params)
        payload = self._profile.transform_response(operation, payload)

        result_path = self._dialect.operation(operation).result_path
        return extract(payload, result_path) if result_path else payload

    async def _call_optional(
        self,
        operation: Operation,
        *,
        path_args: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Lecture dont l'absence est un fait, pas une erreur.

        Une opération déclarée absente du TMS rend `None` sans appel réseau :
        inutile d'interroger un chemin qui n'existe pas. Un 404 est traité de
        même, car certaines ressources sont légitimement vides — un itinéraire
        pas encore planifié, une position jamais reçue.
        """
        if not self.supports(operation):
            return None
        try:
            result = await self._call(operation, path_args=path_args)
        except TmsFailure as failure:
            if failure.error.code is ToolErrorCode.MISSION_NOT_FOUND:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def _call_paginated(
        self,
        operation: Operation,
        *,
        path_args: dict[str, Any] | None = None,
        param_args: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Parcourt toutes les pages d'une liste, selon le style du dialecte."""
        if not self.supports(operation):
            return []

        pagination = self._dialect.pagination
        path, base_params = self._resolve(operation, path_args=path_args, param_args=param_args)
        if pagination.limit_param:
            base_params.setdefault(pagination.limit_param, pagination.page_size)

        items: list[Any] = []
        cursor: str | None = None
        index = 0

        for page_number in range(pagination.max_pages):
            params = dict(base_params)
            if pagination.style is PaginationStyle.CURSOR and cursor:
                params[pagination.cursor_param] = cursor
            elif pagination.style is PaginationStyle.OFFSET:
                params[pagination.offset_param] = index
            elif pagination.style is PaginationStyle.PAGE:
                params[pagination.page_param] = pagination.first_page + page_number

            payload = self._profile.transform_response(operation, await self._get(path, params))
            page_items = extract(payload, pagination.items_path)
            if page_items is None and isinstance(payload, list):
                # Un TMS peut rendre la liste nue, sans enveloppe.
                page_items = payload
            if not isinstance(page_items, list):
                raise TmsFailure.of(
                    ToolErrorCode.TMS_UNAVAILABLE,
                    f"Liste introuvable dans la réponse du TMS sur {path} "
                    f"(chemin attendu : '{pagination.items_path}').",
                    {"resource": path},
                )

            items.extend(page_items)
            index += len(page_items)

            if pagination.style is PaginationStyle.NONE:
                return items
            if not self._has_next_page(payload, page_items, pagination_style=pagination.style):
                return items
            if pagination.style is PaginationStyle.CURSOR:
                cursor = extract(payload, pagination.cursor_path)
                if not cursor:
                    return items

        logger.warning("Pagination interrompue après %d pages sur %s", pagination.max_pages, path)
        return items

    def _has_next_page(
        self,
        payload: Any,
        page_items: list[Any],
        *,
        pagination_style: PaginationStyle,
    ) -> bool:
        pagination = self._dialect.pagination
        if pagination.has_more_path:
            flag = extract(payload, pagination.has_more_path)
            if flag is not None:
                return bool(flag)
        if pagination_style is PaginationStyle.CURSOR:
            return bool(extract(payload, pagination.cursor_path))
        # Offset et numéro de page : une page incomplète est la dernière.
        return len(page_items) >= pagination.page_size

    # --- Lectures métier ------------------------------------------------------
    #  Signatures identiques quel que soit le TMS : c'est le contrat que le
    #  serveur MCP, le monitoring et l'agent connaissent.

    async def health(self) -> dict[str, Any]:
        payload = await self._call(Operation.HEALTH)
        return payload if isinstance(payload, dict) else {}

    async def tms_time(self) -> datetime:
        """Heure courante du TMS (F3), lue à chaque cycle."""
        payload = await self.health()
        raw = extract(payload, self._dialect.their_path(Entity.HEALTH, "time") or "time")
        moment = parse_datetime(raw)
        if moment is None:
            raise TmsFailure.of(
                ToolErrorCode.TMS_UNAVAILABLE,
                "Le TMS ne rend pas son heure courante.",
                {"resource": "health"},
            )
        return moment

    async def list_missions(self, *, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        """Missions du TMS, filtrées sur nos statuts traduits dans son vocabulaire."""
        return await self._call_paginated(
            Operation.LIST_MISSIONS,
            param_args={"statuses": self._their_statuses(statuses)},
        )

    def _their_statuses(self, statuses: list[str] | None) -> list[str] | None:
        """Traduit nos statuts canoniques vers ceux du TMS.

        Sans cette traduction, le filtre serait envoyé dans notre vocabulaire et
        le TMS ne reconnaîtrait rien — au mieux il l'ignorerait et rendrait tout.
        """
        if not statuses:
            return None
        mapping = self._dialect.enums.get("mission_status", {})
        their: list[str] = []
        for canonical in statuses:
            their.extend(mapping.get(canonical, []))
        return their or None

    async def get_mission(self, mission_id: str) -> dict[str, Any]:
        """Détail d'une mission. Son absence est une erreur, pas un `None`."""
        payload = await self._call(Operation.GET_MISSION, path_args={"mission_id": mission_id})
        if not isinstance(payload, dict):
            raise TmsFailure.of(
                ToolErrorCode.TMS_UNAVAILABLE,
                f"Réponse inattendue pour la mission {mission_id}.",
                {"resource": "get_mission", "mission_id": mission_id},
            )
        return self._profile.normalize_mission(payload)

    async def get_mission_events(
        self,
        mission_id: str,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        return await self._call_paginated(
            Operation.GET_MISSION_EVENTS,
            path_args={"mission_id": mission_id},
            param_args={"since": since},
        )

    async def get_route(self, mission_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_ROUTE, path_args={"mission_id": mission_id})

    async def get_deviation(self, mission_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_DEVIATION, path_args={"mission_id": mission_id})

    async def get_eta(self, mission_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_ETA, path_args={"mission_id": mission_id})

    async def get_vehicle(self, vehicle_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_VEHICLE, path_args={"vehicle_id": vehicle_id})

    async def get_driver(self, driver_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_DRIVER, path_args={"driver_id": driver_id})

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        return await self._call_optional(Operation.GET_USER, path_args={"user_id": user_id})

    async def list_users_by_role(self, role: str) -> list[dict[str, Any]]:
        """Utilisateurs portant un rôle, pour résoudre les destinataires (F16)."""
        return await self._call_paginated(Operation.LIST_USERS_BY_ROLE, path_args={"role": role})

    async def get_vehicle_positions(
        self,
        vehicle_id: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return await self._call_paginated(
            Operation.GET_VEHICLE_POSITIONS,
            path_args={"vehicle_id": vehicle_id},
            param_args={"since": since, "limit": limit},
        )
