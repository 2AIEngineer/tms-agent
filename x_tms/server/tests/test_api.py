from tms.geo import decode_polyline

PROBLEM = "application/problem+json"


def test_health_and_openapi(client):
    health = client.get("/v1/health").json()
    assert health["status"] == "ok"
    assert health["simulation"]["running"] is False
    spec = client.get("/v1/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    assert "/v1/missions/{mission_id}/deviation" in spec["paths"]


def test_missions_list_filters_and_pagination(client, simulated):
    body = client.get("/v1/missions", params={"limit": 2}).json()
    assert len(body["data"]) == 2 and body["pagination"]["has_more"] is True
    rest = client.get("/v1/missions", params={"limit": 2, "cursor": body["pagination"]["cursor"]}).json()
    assert len(rest["data"]) == 1 and rest["pagination"] == {"cursor": None, "has_more": False}

    assert len(client.get("/v1/missions", params={"status": "completed"}).json()["data"]) == 3
    assert client.get("/v1/missions", params={"status": "in_progress"}).json()["data"] == []
    only = client.get("/v1/missions", params={"vehicle_id": "veh_202"}).json()["data"]
    assert [m["vehicle_id"] for m in only] == ["veh_202"]
    assert client.get("/v1/missions", params={"updated_since": "2999-01-01T00:00:00Z"}).json()["data"] == []
    assert client.get("/v1/missions", params={"cursor": "garbage"}).status_code == 400


def test_mission_detail_events_route_eta(client, simulated):
    mission_id = simulated.mission_ids[0]
    detail = client.get(f"/v1/missions/{mission_id}").json()
    assert detail["reference"].startswith("MIS-")
    assert detail["origin"]["label"] == "Entrepôt Casablanca"
    assert detail["events"][0]["type"] == "arrived_pickup"
    assert detail["actual_pickup_at"] == detail["events"][0]["occurred_at"]
    assert detail["planned_pickup_at"].endswith("Z")

    events = client.get(f"/v1/missions/{mission_id}/events", params={"type": "loading_completed"}).json()[
        "data"
    ]
    assert len(events) == 1 and events[0]["payload"] == {"weight_kg": 12400}
    later = client.get(
        f"/v1/missions/{mission_id}/events", params={"since": events[0]["occurred_at"]}
    ).json()["data"]
    assert later[0]["type"] == "departed_pickup"

    route = client.get(f"/v1/missions/{mission_id}/route").json()
    assert route["id"] == detail["planned_route_id"]
    assert len(decode_polyline(route["polyline"])) == len(route["waypoints"])
    assert route["distance_km"] > 80 and route["duration_min"] > 60

    eta = client.get(f"/v1/missions/{mission_id}/eta").json()
    assert eta["current_eta"] == detail["actual_delivery_at"]
    assert eta["remaining_distance_km"] == 0

    deviation = client.get(f"/v1/missions/{mission_id}/deviation").json()
    assert deviation["severity"] == "low"


def test_unknown_resources_are_problem_json(client):
    resp = client.get("/v1/missions/msn_nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == PROBLEM
    assert resp.json()["status"] == 404 and resp.json()["instance"] == "/v1/missions/msn_nope"
    assert client.get("/v1/vehicles/veh_nope").status_code == 404
    assert client.get("/v1/drivers/usr_agent_12").status_code == 404  # pas un chauffeur


def test_vehicles_and_positions(client, simulated):
    vehicles = client.get("/v1/vehicles").json()["data"]
    assert {v["id"] for v in vehicles} >= {"veh_201", "veh_202", "veh_203"}
    assert client.get("/v1/vehicles", params={"fleet_id": "flt_nord"}).json()["data"][0]["id"] == "veh_203"
    v = client.get("/v1/vehicles/veh_201").json()
    assert v["status"] == "idle" and v["current_mission_id"] is None

    position = client.get("/v1/vehicles/veh_201/position").json()
    assert position["vehicle_id"] == "veh_201" and position["speed_kmh"] == 0

    history = client.get("/v1/vehicles/veh_201/positions", params={"limit": 10}).json()
    assert len(history["data"]) == 10 and history["pagination"]["has_more"]
    times = [p["recorded_at"] for p in history["data"]]
    assert times == sorted(times)
    since = client.get("/v1/vehicles/veh_201/positions", params={"since": times[-1], "limit": 1}).json()[
        "data"
    ]
    assert since[0]["recorded_at"] > times[-1]
    assert client.get("/v1/vehicles/veh_204/position").status_code == 404


def test_users_roles_drivers(client):
    users = client.get("/v1/users").json()["data"]
    assert {u["management_level"] for u in users} == {"M+0", "M+1", "M+2", "M+3", "M+4"}
    sup = client.get("/v1/users", params={"role": "ops_supervisor"}).json()["data"]
    assert sup == [client.get("/v1/users/usr_sup_04").json()]
    assert sup[0]["management_level"] == "M+2"
    assert len(client.get("/v1/users", params={"management_level": "M+0"}).json()["data"]) == 4
    assert [u["id"] for u in client.get("/v1/roles/director/users").json()["data"]] == ["usr_dir_01"]
    driver = client.get("/v1/drivers/drv_77").json()
    assert driver["status"] == "available" and driver["phone"]


def test_api_is_read_only(client):
    """L'API est consommée en lecture seule (via MCP) : aucune méthode d'écriture exposée."""
    spec = client.get("/v1/openapi.json").json()
    methods = {m for ops in spec["paths"].values() for m in ops}
    assert methods == {"get"}
    for path in ("/v1/alerts", "/v1/webhooks", "/v1/notifications"):
        assert client.get(path).status_code == 404
    resp = client.post("/v1/missions/msn_x/events", json={"type": "driver_message"})
    assert resp.status_code == 405 and resp.headers["content-type"] == PROBLEM


def test_driver_status_during_mission(client):
    from tms.simulator.engine import run_simulation

    seen = {}

    def on_tick(at, runners):
        if "driver" not in seen and runners[0].phase == "transit" and runners[0].progress_km > 0:
            seen["driver"] = client.get("/v1/drivers/drv_77").json()
            seen["vehicle"] = client.get("/v1/vehicles/veh_201").json()
            seen["deviation"] = client.get(f"/v1/missions/{runners[0].mission_id}/deviation").json()

    result = run_simulation(["nominal"], fast_forward=True, seed=2, on_tick=on_tick)
    assert seen["driver"]["status"] == "on_mission"
    assert seen["driver"]["current_mission_id"] == result.mission_ids[0]
    assert seen["vehicle"]["status"] == "moving"
    assert seen["vehicle"]["current_mission_id"] == result.mission_ids[0]
    assert seen["deviation"]["current_offset_km"] < 0.5
    assert client.get("/v1/drivers/drv_77").json()["status"] == "available"
