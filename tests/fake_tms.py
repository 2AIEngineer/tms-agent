"""Faux TMS servi par un transport httpx simulé.

Reproduit fidèlement le contrat `/v1` du TMS de référence : mêmes chemins,
mêmes formes de réponse, mêmes erreurs RFC 7807, même pagination par curseur.

Un double fidèle plutôt qu'un connecteur bouchonné : ce que les tests doivent
vérifier, ce sont précisément la traduction des réponses réelles et le
comportement face aux échecs HTTP. Un faux connecteur ne testerait que nos
propres suppositions.

Il permet en outre de provoquer à volonté ce qu'un vrai TMS ne produit pas sur
commande : délais dépassés, 503, données aberrantes.
"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

TMS_NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

# Sentinelle « paramètre non fourni », à distinguer de `None` qui signifie ici
# « le TMS ne rend pas cette ressource » (il répond 404). Le même piège que
# MISSING vs None dans le moteur de règles : les confondre empêcherait de
# tester l'absence d'un itinéraire ou d'un écart.
_UNSET: Any = object()

PARIS = {"label": "Entrepôt Paris Nord", "lat": 48.9014, "lon": 2.3522}
LYON = {"label": "Plateforme Lyon Sud", "lat": 45.7578, "lon": 4.8320}


def iso(moment: datetime) -> str:
    return moment.isoformat()


def mission_payload(
    mission_id: str = "msn_1",
    *,
    status: str = "in_progress",
    reference: str = "REF-0001",
    with_events: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": mission_id,
        "reference": reference,
        "vehicle_id": "veh_1",
        "driver_id": "drv_1",
        "origin": PARIS,
        "destination": LYON,
        "planned_route_id": "rte_1",
        "status": status,
        "planned_pickup_at": iso(TMS_NOW - timedelta(hours=3)),
        "planned_delivery_at": iso(TMS_NOW + timedelta(hours=2)),
        "actual_pickup_at": iso(TMS_NOW - timedelta(hours=2, minutes=50)),
        "actual_delivery_at": None,
        "current_eta": iso(TMS_NOW + timedelta(hours=2, minutes=35)),
        "created_at": iso(TMS_NOW - timedelta(days=1)),
        "updated_at": iso(TMS_NOW - timedelta(minutes=2)),
    }
    if with_events:
        payload["events"] = [
            event_payload("evt_1", "arrived_pickup", TMS_NOW - timedelta(hours=3)),
            event_payload("evt_2", "loading_completed", TMS_NOW - timedelta(hours=2, minutes=55)),
            event_payload("evt_3", "departed_pickup", TMS_NOW - timedelta(hours=2, minutes=50)),
        ]
    return payload


def event_payload(
    event_id: str,
    event_type: str,
    occurred_at: datetime,
    *,
    lat: float = 48.9014,
    lon: float = 2.3522,
    payload: dict[str, Any] | None = None,
    source: str = "tms",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "mission_id": "msn_1",
        "type": event_type,
        "occurred_at": iso(occurred_at),
        "position": {"lat": lat, "lon": lon},
        "payload": payload or {},
        "source": source,
    }


def page(items: list[Any], *, cursor: str | None = None, has_more: bool = False) -> dict[str, Any]:
    return {"data": items, "pagination": {"cursor": cursor, "has_more": has_more}}


def problem(status: int, title: str, detail: str) -> httpx.Response:
    """Erreur au format RFC 7807, comme le TMS les rend."""
    return httpx.Response(
        status,
        content=json.dumps({"type": "about:blank", "title": title, "status": status, "detail": detail}),
        headers={"content-type": "application/problem+json"},
    )


class FakeTms:
    """Double du TMS, pilotable par les tests.

    `fail_paths` force un statut sur un chemin, `timeout_paths` simule un délai
    dépassé, `fail_times` ne fait échouer que les N premiers appels — ce qui
    permet de vérifier qu'une reprise finit par réussir.
    """

    def __init__(
        self,
        *,
        now: datetime = TMS_NOW,
        missions: list[dict[str, Any]] = _UNSET,
        deviation: dict[str, Any] | None = _UNSET,
        eta: dict[str, Any] | None = _UNSET,
        route: dict[str, Any] | None = _UNSET,
        vehicle: dict[str, Any] | None = _UNSET,
        driver: dict[str, Any] | None = _UNSET,
        users_by_role: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.now = now
        self.missions = missions if missions is not _UNSET else [mission_payload()]
        self.deviation = (
            deviation
            if deviation is not _UNSET
            else {
                "mission_id": "msn_1",
                "current_offset_km": 7.234,
                "current_offset_since": iso(now - timedelta(minutes=25)),
                "duration_minutes": 25.0,
                "severity": "high",
                "vehicle_position": {"lat": 47.0, "lon": 3.5},
                "computed_at": iso(now),
            }
        )
        self.eta = (
            eta
            if eta is not _UNSET
            else {
                "mission_id": "msn_1",
                "planned_delivery_at": iso(now + timedelta(hours=2)),
                "current_eta": iso(now + timedelta(hours=3, minutes=30)),
                "delay_minutes": 90.0,
                "remaining_distance_km": 310.5,
                "computed_at": iso(now),
            }
        )
        self.route = (
            route
            if route is not _UNSET
            else {
                "id": "rte_1",
                "mission_id": "msn_1",
                "waypoints": [
                    {"lat": 48.9014, "lon": 2.3522, "sequence": 0},
                    {"lat": 45.7578, "lon": 4.8320, "sequence": 1},
                ],
                "polyline": "abc",
                "distance_km": 465.2,
                "duration_min": 300.0,
            }
        )
        self.vehicle = (
            vehicle
            if vehicle is not _UNSET
            else {
                "id": "veh_1",
                "plate": "AB-123-CD",
                "fleet_id": "flt_1",
                "current_position": {
                    "lat": 47.0,
                    "lon": 3.5,
                    "heading": 170.0,
                    "speed_kmh": 82.0,
                    "recorded_at": iso(now),
                },
                "status": "moving",
                "last_seen_at": iso(now),
                "current_mission_id": "msn_1",
            }
        )
        self.driver = (
            driver
            if driver is not _UNSET
            else {
                "id": "drv_1",
                "name": "Awa Traoré",
                "role": "driver",
                "management_level": "M+0",
                "phone": "+33600000001",
                "email": "awa@example.com",
                "status": "on_mission",
                "current_mission_id": "msn_1",
            }
        )
        self.users_by_role = users_by_role or {
            "ops_agent": [_user("usr_10", "Léa Martin", "ops_agent", "M+1")],
            "ops_supervisor": [_user("usr_20", "Marc Dubois", "ops_supervisor", "M+2")],
            "ops_manager": [_user("usr_30", "Ines Rossi", "ops_manager", "M+3")],
            "director": [_user("usr_40", "Paul Nadeau", "director", "M+4")],
        }
        self.positions = [
            {
                "lat": 47.0 + index * 0.01,
                "lon": 3.5,
                "heading": 170.0,
                "speed_kmh": 80.0,
                "recorded_at": iso(now - timedelta(minutes=10 - index)),
                "mission_id": "msn_1",
            }
            for index in range(10)
        ]

        # Certains TMS tiers ignorent un paramètre de requête qu'ils ne gèrent
        # pas et renvoient tout. Le reproduire permet de vérifier que nous ne
        # surveillons pas des missions que nous n'avons pas demandées.
        self.ignore_status_filter = False

        self.fail_paths: dict[str, int] = {}
        self.timeout_paths: set[str] = set()
        self.fail_times: dict[str, int] = {}
        self.calls: Counter[str] = Counter()
        self.methods: Counter[str] = Counter()

    # --- Pilotage par les tests ----------------------------------------------

    def fail(self, path: str, status: int, *, times: int | None = None) -> None:
        self.fail_paths[path] = status
        if times is not None:
            self.fail_times[path] = times

    def timeout(self, path: str) -> None:
        self.timeout_paths.add(path)

    def recover(self) -> None:
        """Rétablit le TMS : utile pour vérifier qu'une reprise finit par aboutir."""
        self.fail_paths.clear()
        self.fail_times.clear()
        self.timeout_paths.clear()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self, base_url: str = "http://tms.test/v1") -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=self.transport())

    # --- Routage ---------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.calls[path] += 1
        self.methods[request.method] += 1

        if path in self.timeout_paths:
            raise httpx.ReadTimeout("délai dépassé (simulé)", request=request)

        forced = self.fail_paths.get(path)
        if forced is not None:
            remaining = self.fail_times.get(path)
            if remaining is None or self.calls[path] <= remaining:
                return problem(forced, "Erreur simulée", f"Échec forcé sur {path}.")

        return self._route(path, request)

    def _route(self, path: str, request: httpx.Request) -> httpx.Response:
        if path == "/health":
            return _json(
                {
                    "status": "ok",
                    "version": "0.1.0",
                    "time": iso(self.now),
                    "simulation": {"running": True, "run_id": "run_1", "speed": 60.0},
                }
            )

        if path == "/missions":
            statuses = request.url.params.get_list("status")
            missions = [
                mission
                for mission in self.missions
                if self.ignore_status_filter or not statuses or mission["status"] in statuses
            ]
            return _json(page([_without_events(mission) for mission in missions]))

        for mission in self.missions:
            # Une mission sans identifiant est un payload que le code sous test
            # doit tolérer : le double ne doit donc pas lever dessus.
            mission_id = mission.get("id")
            if not mission_id:
                continue
            if path == f"/missions/{mission_id}":
                return _json(mission)
            if path == f"/missions/{mission_id}/events":
                return _json(page(mission.get("events", [])))
            if path == f"/missions/{mission_id}/deviation":
                return _maybe(self.deviation, "Écart")
            if path == f"/missions/{mission_id}/eta":
                return _maybe(self.eta, "ETA")
            if path == f"/missions/{mission_id}/route":
                return _maybe(self.route, "Itinéraire")

        if path.startswith("/missions/"):
            return problem(404, "Mission introuvable", f"Aucune mission pour {path!r}.")

        if path == "/vehicles/veh_1":
            return _maybe(self.vehicle, "Véhicule")
        if path == "/vehicles/veh_1/positions":
            return _json(page(self.positions))
        if path == "/drivers/drv_1":
            return _maybe(self.driver, "Chauffeur")

        if path.startswith("/roles/") and path.endswith("/users"):
            role = path.removeprefix("/roles/").removesuffix("/users")
            return _json(page(self.users_by_role.get(role, [])))

        if path.startswith("/users/"):
            user_id = path.removeprefix("/users/")
            for users in self.users_by_role.values():
                for user in users:
                    if user["id"] == user_id:
                        return _json(user)
            return problem(404, "Utilisateur introuvable", f"Aucun utilisateur {user_id!r}.")

        return problem(404, "Ressource introuvable", f"Chemin inconnu : {path}")


def _user(user_id: str, name: str, role: str, level: str, *, phone: str | None = "+33600000000") -> dict:
    return {
        "id": user_id,
        "name": name,
        "role": role,
        "management_level": level,
        "phone": phone,
        "email": f"{user_id}@example.com",
    }


def _without_events(mission: dict[str, Any]) -> dict[str, Any]:
    """La liste des missions ne porte pas les événements, contrairement au détail."""
    return {key: value for key, value in mission.items() if key != "events"}


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _maybe(payload: dict[str, Any] | None, label: str) -> httpx.Response:
    """Une ressource absente répond 404, comme le vrai TMS."""
    if payload is None:
        return problem(404, f"{label} introuvable", f"Aucun {label.lower()} disponible.")
    return _json(payload)
