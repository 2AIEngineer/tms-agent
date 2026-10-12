import base64

from sqlalchemy import Select
from sqlalchemy.orm import Session

from tms.errors import ProblemError
from tms.schemas import Pagination

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _encode(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, value = raw.split(":", 1)
        offset = int(value)
        if kind != "o" or offset < 0:
            raise ValueError
        return offset
    except ValueError as exc:
        raise ProblemError(400, "Curseur invalide", f"Le curseur {cursor!r} est invalide.") from exc


def paginate(session: Session, stmt: Select, limit: int, cursor: str | None) -> tuple[list, Pagination]:
    """Pagination par curseur opaque. `stmt` doit avoir un ORDER BY déterministe."""
    offset = _decode(cursor) if cursor else 0
    rows = list(session.scalars(stmt.offset(offset).limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]
    return rows, Pagination(cursor=_encode(offset + limit) if has_more else None, has_more=has_more)
