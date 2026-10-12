from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from tms.db import get_session
from tms.pagination import DEFAULT_LIMIT, MAX_LIMIT

SessionDep = Annotated[Session, Depends(get_session)]
LimitParam = Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Taille de page.")]
CursorParam = Annotated[str | None, Query(description="Curseur renvoyé par la page précédente.")]
DEFAULT = DEFAULT_LIMIT
