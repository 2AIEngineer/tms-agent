"""Horloge du TMS.

Le simulateur peut accélérer le temps (`--speed`). Pour que les durées lues via
l'API (âge du dernier événement, durée d'une dérive, ETA...) restent cohérentes,
l'API et le simulateur lisent tous deux l'heure via cette horloge, persistée en
base : `now = sim_anchor + (heure_réelle - real_anchor) * speed`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from tms.models import SimulationClock


def real_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClockState:
    real_anchor: datetime
    sim_anchor: datetime
    speed: float
    running: bool
    run_id: str | None

    def now(self) -> datetime:
        elapsed = real_now() - self.real_anchor
        return (self.sim_anchor + elapsed * self.speed).replace(microsecond=0)


def _row(session: Session) -> SimulationClock | None:
    return session.get(SimulationClock, 1)


def get_clock(session: Session) -> ClockState:
    row = _row(session)
    if row is None:
        t = real_now()
        return ClockState(t, t, 1.0, False, None)
    return ClockState(row.real_anchor, row.sim_anchor, row.speed, row.running, row.run_id)


def now(session: Session) -> datetime:
    return get_clock(session).now()


def set_clock(
    session: Session, *, speed: float, running: bool, run_id: str | None, anchor: datetime | None = None
) -> ClockState:
    """Ré-ancre l'horloge à l'instant courant avec une nouvelle vitesse.

    L'heure simulée ne recule jamais : on repart de max(heure réelle, heure simulée, `anchor`).
    """
    anchor = max(now(session), real_now().replace(microsecond=0), anchor or real_now())
    row = _row(session)
    if row is None:
        row = SimulationClock(id=1)
        session.add(row)
    row.real_anchor = real_now()
    row.sim_anchor = anchor
    row.speed = speed
    row.running = running
    row.run_id = run_id
    session.flush()
    return get_clock(session)


def reset_clock(session: Session) -> None:
    """Revient à l'heure réelle (après une remise à zéro des données)."""
    row = _row(session)
    if row is not None:
        session.delete(row)
        session.flush()
