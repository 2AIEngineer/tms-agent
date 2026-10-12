"""Double HTTP de « FleetLink », TMS fictif aux conventions opposées.

Il sert à un seul test, mais le plus important de toute la frontière : si les
mêmes situations métier passent sur ce TMS et sur celui de référence, alors
brancher un TMS est bien devenu un travail de configuration.

Tout y est délibérément différent : préfixe d'URL, noms d'endpoints, noms de
champs, imbrication, enveloppes de réponse, pagination par numéro de page,
authentification par en-tête, statuts en majuscules — et une ressource
purement absente, l'écart à l'itinéraire.
"""

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FLEET_NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)

# Mêmes lieux que le double de référence, pour que les distances dérivées
# soient comparables d'un TMS à l'autre.
FROM_PLACE = {"name": "Entrepôt Paris Nord", "y": 48.9014, "x": 2.3522}
TO_PLACE = {"name": "Plateforme Lyon Sud", "y": 45.7578, "x": 4.8320}


def iso(moment: datetime) -> str:
    # FleetLink suffixe ses dates par « Z » là où le TMS de référence rend un
    # décalage explicite : une variation de plus, et le mapper doit l'absorber.
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def transport_payload(
    mission_id: str = "T-77001",
    *,
    state: str = "RUNNING",
    reference: str = "FL-2026-77001",
    with_timeline: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mss_id": mission_id,
        "thread_ref": reference,
        "state": state,
        "truck": {"ref": "TRK-9"},
        "people": {"driver_ref": "PPL-41"},
        "from": FROM_PLACE,
        "to": TO_PLACE,
        "schedule": {
            "load_due": iso(FLEET_NOW - timedelta(hours=3)),
            "drop_due": iso(FLEET_NOW + timedelta(hours=2)),
            "load_real": iso(FLEET_NOW - timedelta(hours=2, minutes=50)),
            "drop_real": None,
        },
    }
    if with_timeline:
        payload["timeline"] = [
            timeline_payload("E-1", "PICKUP_ARRIVAL", FLEET_NOW - timedelta(hours=3)),
            timeline_payload("E-2", "LOAD_END", FLEET_NOW - timedelta(hours=2, minutes=55)),
            timeline_payload("E-3", "PICKUP_DEPARTURE", FLEET_NOW - timedelta(hours=2, minutes=50)),
        ]
    return payload


def timeline_payload(
    event_id: str,
    kind: str,
    occurred_at: datetime,
    *,
    lat: float = 48.9014,
    lon: float = 2.3522,
    extra: dict[str, Any] | None = None,
    emitter: str = "BACKOFFICE",
) -> dict[str, Any]:
    return {
        "evt_uuid": event_id,
        "mss_id": "T-77001",
        "kind": kind,
        "ts": iso(occurred_at),
        "geo": {"y": lat, "x": lon},
        "extra": extra or {},
        "emitter": emitter,
    }


def envelope(obj: Any) -> dict[str, Any]:
    """FleetLink enveloppe ses objets simples sous `result`."""
    return {"result": obj}


def page(items: list[Any], *, more: bool = False) -> dict[str, Any]:
    """Et ses listes sous `items`, avec un drapeau `meta.more`."""
    return {"items": items, "meta": {"more": more}}


def problem(status: int, detail: str) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps({"detail": detail}), headers={"content-type": "application/json"}
    )


class FakeFleetLink:
    """Double de FleetLink, pilotable par les tests."""

    def __init__(self, *, now: datetime = FLEET_NOW, transports: list[dict[str, Any]] | None = None) -> None:
        self.now = now
        self.transports = transports if transports is not None else [transport_payload()]
        self.forecast: dict[str, Any] | None = {
            "due_at": iso(now + timedelta(hours=2)),
            "forecast_at": iso(now + timedelta(hours=3, minutes=30)),
            "lateness_min": 90,
            "left_km": 310.5,
            "as_of": iso(now),
        }
        self.way: dict[str, Any] | None = {
            "total_km": 465.2,
            "total_min": 300,
            "geometry": {"points": [{"y": 48.9014, "x": 2.3522}, {"y": 45.7578, "x": 4.8320}]},
        }
        self.truck: dict[str, Any] | None = {
            "ref": "TRK-9",
            "registration": "AB-123-CD",
            "state": "ROLLING",
            "last_ping": iso(now),
            "geo": {"y": 47.0, "x": 3.5, "bearing": 170.0, "velocity": 82.0, "measured_at": iso(now)},
        }
        self.driver: dict[str, Any] | None = {
            "ref": "PPL-41",
            "full_name": "Awa Traoré",
            "job": "driver",
            "tier": "M+0",
            "contact": {"mobile": "+33600000001", "mail": "awa@example.com"},
        }
        self.people_by_job: dict[str, list[dict[str, Any]]] = {
            "ops_agent": [_person("PPL-10", "Léa Martin", "ops_agent", "M+1")],
            "ops_supervisor": [_person("PPL-20", "Marc Dubois", "ops_supervisor", "M+2")],
            "ops_manager": [_person("PPL-30", "Inès Rossi", "ops_manager", "M+3")],
            "director": [_person("PPL-40", "Paul Nadeau", "director", "M+4")],
        }
        self.trace = [
            {"y": 47.0 + index * 0.01, "x": 3.5, "measured_at": iso(now - timedelta(minutes=10 - index))}
            for index in range(10)
        ]
        self.calls: Counter[str] = Counter()
        self.methods: Counter[str] = Counter()
        self.api_keys: list[str | None] = []

    def transport(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://fleetlink.test", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[path] += 1
        self.methods[request.method] += 1
        self.api_keys.append(request.headers.get("X-Api-Key"))

        if path == "/resources/ping":
            return _json(envelope({"server_clock": iso(self.now), "build": "fleetlink-3"}))

        if path == "/resources/transports":
            states = request.url.params.get_list("state")
            selected = [t for t in self.transports if not states or t["state"] in states]
            return _json(page([_without_timeline(t) for t in selected]))

        for transport in self.transports:
            mission_id = transport["mss_id"]
            if path == f"/resources/transports/{mission_id}":
                return _json(envelope(transport))
            if path == f"/resources/transports/{mission_id}/timeline":
                return _json(page(transport.get("timeline", [])))
            if path == f"/resources/transports/{mission_id}/way":
                return _maybe_envelope(self.way)
            if path == f"/resources/transports/{mission_id}/forecast":
                return _maybe_envelope(self.forecast)

        if path.startswith("/resources/transports/"):
            return problem(404, f"Unknown transport for {path!r}.")

        if path == "/resources/trucks/TRK-9":
            return _maybe_envelope(self.truck)
        if path == "/resources/trucks/TRK-9/trace":
            return _json(page(self.trace))

        if path == "/resources/people":
            job = request.url.params.get("job")
            return _json(page(self.people_by_job.get(job or "", [])))

        if path.startswith("/resources/people/"):
            person_id = path.removeprefix("/resources/people/")
            if self.driver and person_id == self.driver["ref"]:
                return _maybe_envelope(self.driver)
            for people in self.people_by_job.values():
                for person in people:
                    if person["ref"] == person_id:
                        return _json(envelope(person))
            return problem(404, f"Unknown person {person_id!r}.")

        return problem(404, f"Unknown path {path}")


def _person(ref: str, name: str, job: str, tier: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "full_name": name,
        "job": job,
        "tier": tier,
        "contact": {"mobile": "+33600000000", "mail": f"{ref.lower()}@example.com"},
    }


def _without_timeline(transport: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in transport.items() if key != "timeline"}


def _json(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _maybe_envelope(payload: dict[str, Any] | None) -> httpx.Response:
    if payload is None:
        return problem(404, "Resource not available.")
    return _json(envelope(payload))
