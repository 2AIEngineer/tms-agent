"""Erreurs structurées des tools MCP.

Un LLM ne doit jamais confondre « la mission n'existe pas » avec « je n'ai pas
réussi à lire la mission » : la première est une information métier, la seconde
un incident technique. Une erreur en texte libre rend cette distinction
indevinable, d'où un code fermé et un drapeau `retryable` explicite.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolErrorCode(StrEnum):
    MISSION_NOT_FOUND = "MISSION_NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TMS_TIMEOUT = "TMS_TIMEOUT"
    TMS_RATE_LIMIT = "TMS_RATE_LIMIT"
    TMS_UNAVAILABLE = "TMS_UNAVAILABLE"
    PARTIAL_DATA = "PARTIAL_DATA"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    NOT_PERMITTED = "NOT_PERMITTED"


# Ce qu'il est utile de retenter : une indisponibilité passagère du TMS, pas une
# mission absente ni un argument invalide.
_RETRYABLE: frozenset[ToolErrorCode] = frozenset(
    {
        ToolErrorCode.TMS_TIMEOUT,
        ToolErrorCode.TMS_RATE_LIMIT,
        ToolErrorCode.TMS_UNAVAILABLE,
    }
)


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ToolErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] | None = None

    @classmethod
    def of(
        cls,
        code: ToolErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> "ToolError":
        """Construit l'erreur en déduisant `retryable` du code.

        Évite qu'un appelant déclare retryable une erreur qui ne l'est pas, ce
        qui ferait boucler l'agent sur une mission inexistante.
        """
        return cls(code=code, message=message, retryable=code in _RETRYABLE, details=details)
