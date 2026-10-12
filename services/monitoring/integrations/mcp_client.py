"""Client MCP du service de surveillance.

Le monitoring ne lit jamais le TMS directement : il passe par le serveur MCP,
comme l'agent. Ce client est donc sa seule porte vers l'extérieur.

Il reste volontairement étroit : il n'expose que les capacités dont la
surveillance a besoin, et traduit le contrat d'erreur des outils en exceptions
typées. Un client MCP générique obligerait chaque appelant à réinterpréter
`ok`, `code` et `retryable` — et un seul oubli suffirait à prendre une panne
technique pour une information métier.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from pydantic import ValidationError

from shared.mapped_dto import MissionSnapshot, NotificationChannel, ToolError, ToolErrorCode

logger = logging.getLogger(__name__)


class McpCallFailed(Exception):
    """Un outil MCP a rendu une erreur, ou le serveur est injoignable."""

    def __init__(self, error: ToolError) -> None:
        super().__init__(f"{error.code.value}: {error.message}")
        self.error = error

    @property
    def retryable(self) -> bool:
        return self.error.retryable


@dataclass(slots=True)
class CycleHarvest:
    """Récolte d'un cycle, déjà traduite en objets pivot.

    Les instantanés arrivent du serveur MCP sous forme JSON ; ils sont
    revalidés ici, à la frontière, pour que le moteur de règles ne reçoive
    jamais un dictionnaire approximatif. Un instantané qui ne se revalide pas
    est compté en échec plutôt que silencieusement déformé.
    """

    # Une heure par TMS : elles n'ont aucune raison de coïncider, et chaque
    # instantané est jugé sur celle de son propre TMS (F3).
    tms_time: dict[str, datetime]
    snapshots: list[MissionSnapshot]
    # Indexés par `tms_id:mission_id` : deux TMS peuvent porter une mission du
    # même identifiant, et les confondre masquerait l'un des deux échecs.
    failures: dict[str, ToolError]
    truncated: bool = False
    tms_requests: dict[str, Any] = field(default_factory=dict)

    @property
    def partial(self) -> list[MissionSnapshot]:
        return [snapshot for snapshot in self.snapshots if not snapshot.snapshot_complete]

    @property
    def tms_ids(self) -> list[str]:
        return sorted(self.tms_time)

    def observed_at(self, tms_id: str) -> datetime | None:
        return self.tms_time.get(tms_id)


@dataclass(frozen=True, slots=True)
class ConnectedTms:
    """Un TMS raccordé, tel que le serveur MCP le déclare."""

    tms_id: str
    label: str
    verdict: str
    # Règles que ce TMS rend inopérantes : à journaliser au démarrage, pour
    # qu'une absence d'alerte ne passe pas pour une absence d'anomalie.
    inoperative_rules: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return self.verdict != "ok"


@dataclass(frozen=True, slots=True)
class ResolvedRecipient:
    """Destinataire résolu dans le TMS au moment de l'envoi (F16)."""

    user_id: str
    name: str
    management_level: str
    address: str | None
    reachable: bool
    unreachable_reason: str | None


def unwrap(result: CallToolResult) -> dict[str, Any]:
    """Extrait la charge utile d'un appel d'outil.

    FastMCP enveloppe un résultat non modélisé sous `result` ; la forme est
    normalisée ici, en un seul endroit, pour qu'aucun appelant n'ait à le savoir.
    """
    payload = result.structured_content
    if isinstance(payload, dict):
        inner = payload.get("result", payload)
        if isinstance(inner, dict):
            return inner
    if result.content:
        decoded = json.loads(result.content[0].text)  # type: ignore[union-attr]
        if isinstance(decoded, dict):
            return decoded
    raise McpCallFailed(ToolError.of(ToolErrorCode.TMS_UNAVAILABLE, "Réponse MCP illisible", {"tool": "?"}))


def ensure_ok(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Transforme un `ok: false` en exception portant le code d'origine."""
    if payload.get("ok") is False:
        error = payload.get("error") or {}
        code = ToolErrorCode(error.get("code", ToolErrorCode.TMS_UNAVAILABLE.value))
        raise McpCallFailed(
            ToolError.of(code, str(error.get("message") or f"échec de {tool}"), {"tool": tool})
        )
    return payload


class McpClient:
    """Accès aux outils MCP, pour le cycle de surveillance et l'envoi.

    `target` est l'URL du serveur MCP en exploitation, ou une instance de
    serveur en mémoire dans les tests : le transport est choisi par FastMCP, et
    le code appelant est identique dans les deux cas.
    """

    def __init__(self, target: Any) -> None:
        self._client = Client(target)

    async def __aenter__(self) -> "McpClient":
        # `Client` n'est pas annoté : les appels sont explicitement tolérés ici
        # plutôt que de relâcher la rigueur de typage sur tout le module.
        await self._client.__aenter__()  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.__aexit__(*exc_info)  # type: ignore[no-untyped-call]

    async def _call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        try:
            result = await self._client.call_tool(tool, arguments)
        except Exception as exception:
            # Serveur MCP injoignable : incident technique à réessayer, et non
            # une information sur la mission (§33, MCP indisponible).
            logger.warning("Appel MCP '%s' en échec : %s", tool, exception)
            raise McpCallFailed(
                ToolError.of(
                    ToolErrorCode.TMS_UNAVAILABLE,
                    f"Serveur MCP injoignable pour '{tool}' : {exception}",
                    {"tool": tool},
                )
            ) from exception
        return ensure_ok(tool, unwrap(result))

    async def resolve_recipients(
        self,
        *,
        channel: NotificationChannel,
        tms_id: str,
        role: str | None = None,
        user_id: str | None = None,
        mission_id: str | None = None,
    ) -> list[ResolvedRecipient]:
        """Qui prévenir pour ce rôle — ou cette personne — et ce canal (F16).

        `tms_id` est indispensable dès qu'un second TMS est raccordé : chaque
        TMS a son propre annuaire, et un superviseur n'est pas interchangeable
        d'un client à l'autre.
        """
        payload = await self._call(
            "resolve_recipients",
            channel=channel.value,
            role=role,
            user_id=user_id,
            mission_id=mission_id,
            tms_id=tms_id,
        )
        return [
            ResolvedRecipient(
                user_id=str(item["user_id"]),
                name=str(item.get("name") or item["user_id"]),
                management_level=str(item.get("management_level") or "M+0"),
                address=item.get("address"),
                reachable=bool(item.get("reachable")),
                unreachable_reason=item.get("unreachable_reason"),
            )
            for item in payload.get("recipients", [])
        ]

    async def get_mission_snapshot(self, tms_id: str, mission_id: str) -> MissionSnapshot | None:
        """Instantané d'une mission, dans le TMS nommé."""
        payload = await self._call("get_mission_snapshot", mission_id=mission_id, tms_id=tms_id)
        raw = payload.get("snapshot")
        return _parse_snapshot(raw) if isinstance(raw, dict) else None

    async def collect_cycle(self, tms_id: str) -> CycleHarvest:
        """Récolte d'un cycle en un seul appel MCP (F2).

        Un appel et non 1 + N : relire l'heure du TMS pour chaque mission
        ferait juger deux missions du même cycle sur des instants différents.

        La récolte porte sur **un** TMS : c'est l'unité de travail du worker,
        qui en surveille un par boucle. Un TMS lent ou en panne ne doit pas
        retarder le cycle d'un autre.
        """
        payload = await self._call("get_monitoring_cycle", tms_id=tms_id)

        snapshots: list[MissionSnapshot] = []
        failures = {
            mission_id: ToolError.of(
                ToolErrorCode(error.get("code", ToolErrorCode.TMS_UNAVAILABLE.value)),
                str(error.get("message") or "mission illisible"),
            )
            for mission_id, error in (payload.get("failures") or {}).items()
        }

        for raw in payload.get("snapshots") or []:
            parsed = _parse_snapshot(raw)
            if parsed is None:
                key = (
                    f"{raw.get('tms_id', '?')}:{raw.get('mission_id', '?')}"
                    if isinstance(raw, dict)
                    else "?:?"
                )
                failures[key] = ToolError.of(
                    ToolErrorCode.PARTIAL_DATA,
                    "Instantané non conforme au contrat pivot",
                )
                continue
            snapshots.append(parsed)

        return CycleHarvest(
            tms_time=_parse_clocks(payload.get("tms_time")),
            snapshots=snapshots,
            failures=failures,
            truncated=bool(payload.get("truncated")),
            tms_requests=payload.get("tms_requests") or {},
        )

    async def get_active_missions(self, tms_id: str) -> dict[str, Any]:
        return await self._call("get_active_missions", tms_id=tms_id)

    async def tms_time(self, tms_id: str) -> str | None:
        payload = await self._call("get_tms_time", tms_id=tms_id)
        value = payload.get("tms_time")
        return str(value) if value else None

    async def list_tms(self) -> list[ConnectedTms]:
        """TMS raccordés, découverts auprès du serveur MCP.

        Le worker ne lit pas la configuration des connexions : elle appartient
        au serveur MCP, qui est le seul à savoir quels TMS sont joignables et
        avec quelles capacités.
        """
        payload = await self._call("list_tms")
        return [
            ConnectedTms(
                tms_id=str(entry["tms_id"]),
                label=str(entry.get("label") or entry["tms_id"]),
                verdict=str(entry.get("verdict") or "ok"),
                inoperative_rules=tuple(
                    str(rule.get("rule_id")) for rule in entry.get("inoperative_rules") or []
                ),
            )
            for entry in payload.get("tms", [])
        ]


def _parse_snapshot(raw: dict[str, Any]) -> MissionSnapshot | None:
    """Revalide un instantané venu du serveur MCP."""
    try:
        return MissionSnapshot.model_validate(raw)
    except ValidationError as error:
        logger.error(
            "Instantané rejeté pour la mission %s : %s",
            raw.get("mission_id", "?"),
            error.errors()[0].get("msg") if error.errors() else error,
        )
        return None


def _parse_clocks(value: Any) -> dict[str, datetime]:
    """Heures des TMS récoltés, une par connexion.

    Un cycle sans aucune heure exploitable n'a pas de référence temporelle et
    ne peut rien décider : c'est un échec, et non une récolte vide (F3, F5).
    Une heure illisible sur un TMS parmi plusieurs est en revanche signalée
    sans priver les autres de surveillance.
    """
    clocks: dict[str, datetime] = {}
    if isinstance(value, dict):
        for tms_id, raw in value.items():
            if not isinstance(raw, str):
                continue
            try:
                clocks[str(tms_id)] = datetime.fromisoformat(raw)
            except ValueError:
                logger.error("Heure illisible pour le TMS %s : %r", tms_id, raw)

    if not clocks:
        raise McpCallFailed(
            ToolError.of(
                ToolErrorCode.TMS_UNAVAILABLE,
                f"Aucune heure de TMS exploitable dans la récolte : {value!r}",
            )
        )
    return clocks
