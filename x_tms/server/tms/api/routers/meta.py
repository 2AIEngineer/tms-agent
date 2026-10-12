from fastapi import APIRouter

from tms import __version__
from tms.api.deps import SessionDep
from tms.clock import get_clock
from tms.schemas import HealthOut, SimulationInfo

router = APIRouter(tags=["Meta"])


@router.get("/health", response_model=HealthOut, summary="Statut du service")
def health(session: SessionDep):
    clock = get_clock(session)
    return HealthOut(
        status="ok",
        version=__version__,
        time=clock.now(),
        simulation=SimulationInfo(running=clock.running, run_id=clock.run_id, speed=clock.speed),
    )
