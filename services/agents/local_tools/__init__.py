"""Outils servis par le service agent lui-même, et non par MCP.

Deux lectures que la spécification accorde à l'enquêteur (F21) et qui ne
relèvent pas du TMS :

  - **notre historique** sur la mission — nos alertes, nos enquêtes, nos
    appels. Le serveur MCP est la frontière du TMS, pas celle de notre base ;
  - **la réévaluation des règles** sur l'état courant, par le moteur même qui
    a créé l'alerte.

`call_driver` suit la même logique mais vit dans `operations/` : c'est une
action, et elle appelle des garde-fous que ces lectures n'ont pas.
"""

from .history import HISTORY_TOOL_NAME, AgentHistoryTool, read_history
from .rules import RULES_TOOL_NAME, RuleCheckTool

__all__ = [
    "HISTORY_TOOL_NAME",
    "RULES_TOOL_NAME",
    "AgentHistoryTool",
    "RuleCheckTool",
    "read_history",
]
