"""Données de référence : utilisateurs de test (un par niveau de management) et flotte."""

from tms.db import session_scope
from tms.models import User, Vehicle

USERS = [
    # Chauffeurs (M+0) : un par véhicule pour faire rouler les missions en parallèle.
    dict(
        id="drv_77",
        name="Karim El Amrani",
        role="driver",
        management_level="M+0",
        phone="+212600000077",
        email="karim.elamrani@example.com",
    ),
    dict(
        id="drv_78",
        name="Mehdi Tazi",
        role="driver",
        management_level="M+0",
        phone="+212600000078",
        email="mehdi.tazi@example.com",
    ),
    dict(
        id="drv_79",
        name="Hamid Ouali",
        role="driver",
        management_level="M+0",
        phone="+212600000079",
        email="hamid.ouali@example.com",
    ),
    dict(
        id="drv_80",
        name="Rachid Bennani",
        role="driver",
        management_level="M+0",
        phone="+212600000080",
        email="rachid.bennani@example.com",
    ),
    dict(
        id="usr_agent_12",
        name="Salma R.",
        role="ops_agent",
        management_level="M+1",
        phone="+212600000012",
        email="salma.r@example.com",
    ),
    dict(
        id="usr_sup_04",
        name="Yassine B.",
        role="ops_supervisor",
        management_level="M+2",
        phone="+212600000004",
        email="yassine.b@example.com",
    ),
    dict(
        id="usr_mgr_02",
        name="Nadia K.",
        role="ops_manager",
        management_level="M+3",
        phone="+212600000002",
        email="nadia.k@example.com",
    ),
    dict(
        id="usr_dir_01",
        name="Omar F.",
        role="director",
        management_level="M+4",
        phone="+212600000001",
        email="omar.f@example.com",
    ),
]

VEHICLES = [
    dict(id="veh_201", plate="AB-123-CD", fleet_id="flt_casa"),
    dict(id="veh_202", plate="EF-456-GH", fleet_id="flt_casa"),
    dict(id="veh_203", plate="IJ-789-KL", fleet_id="flt_nord"),
    dict(id="veh_204", plate="MN-012-OP", fleet_id="flt_casa"),
]


def seed_reference_data() -> None:
    with session_scope() as session:
        for data in USERS:
            session.merge(User(**data))
        for data in VEHICLES:
            vehicle = session.get(Vehicle, data["id"])
            if vehicle is None:
                session.add(Vehicle(status="idle", **data))
            else:
                vehicle.plate, vehicle.fleet_id = data["plate"], data["fleet_id"]
