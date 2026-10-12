"""Assistant opérateur : questions, conversations, rapports (F24 à F26).

Workflow distinct de l'enquêteur, avec lequel il partage le client MCP, le
client d'inférence et les contrats — mais rien de sa logique.
"""

from .actions import AssistantActions
from .pending import REQUIRES_CONFIRMATION, PendingActionRepository
from .repository import ConversationRepository
from .service import AssistantService
from .tools import ASSISTANT_READ_TOOLS

__all__ = [
    "ASSISTANT_READ_TOOLS",
    "REQUIRES_CONFIRMATION",
    "AssistantActions",
    "AssistantService",
    "ConversationRepository",
    "PendingActionRepository",
]
