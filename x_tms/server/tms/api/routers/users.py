from fastapi import APIRouter
from sqlalchemy import select

from tms.api.deps import DEFAULT, CursorParam, LimitParam, SessionDep
from tms.errors import not_found
from tms.models import Mission, User
from tms.pagination import paginate
from tms.schemas import DriverOut, Page, UserOut

router = APIRouter(tags=["Chauffeurs et utilisateurs"])


@router.get("/drivers/{driver_id}", response_model=DriverOut, summary="Détail d'un chauffeur")
def get_driver(driver_id: str, session: SessionDep):
    user = session.get(User, driver_id)
    if user is None or user.role != "driver":
        raise not_found("Chauffeur", driver_id)
    mission = session.scalars(
        select(Mission).where(Mission.driver_id == driver_id, Mission.status == "in_progress")
    ).first()
    return DriverOut(
        **UserOut.model_validate(user).model_dump(),
        status="on_mission" if mission else "available",
        current_mission_id=mission.id if mission else None,
    )


@router.get("/users", response_model=Page[UserOut], summary="Lister les utilisateurs")
def list_users(
    session: SessionDep,
    role: str | None = None,
    management_level: str | None = None,
    limit: LimitParam = DEFAULT,
    cursor: CursorParam = None,
):
    stmt = select(User).order_by(User.management_level, User.id)
    if role:
        stmt = stmt.where(User.role == role)
    if management_level:
        stmt = stmt.where(User.management_level == management_level)
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[UserOut.model_validate(u) for u in rows], pagination=pagination)


@router.get("/users/{user_id}", response_model=UserOut, summary="Détail d'un utilisateur")
def get_user(user_id: str, session: SessionDep):
    user = session.get(User, user_id)
    if user is None:
        raise not_found("Utilisateur", user_id)
    return UserOut.model_validate(user)


@router.get(
    "/roles/{role}/users",
    response_model=Page[UserOut],
    summary="Utilisateurs d'un rôle",
)
def list_role_users(
    role: str,
    session: SessionDep,
    limit: LimitParam = DEFAULT,
    cursor: CursorParam = None,
):
    stmt = select(User).where(User.role == role).order_by(User.id)
    rows, pagination = paginate(session, stmt, limit, cursor)
    return Page(data=[UserOut.model_validate(u) for u in rows], pagination=pagination)
