"""Outils de l'assistant : ce qu'il peut lire, ce qu'il peut faire.

Deux listes, et la séparation est le cœur du sujet (F26) :

  - **les lectures** sont toujours disponibles. Elles ne changent rien et
    coûtent au pire un appel au TMS ;
  - **les actions** ne sont présentées que si l'opérateur a explicitement
    autorisé d'agir. Un outil absent ne peut pas être appelé par zèle.

La liste des lectures est plus large que celle de l'enquêteur : un opérateur
demande « où en sont mes missions ? », question qui porte sur la flotte et non
sur une mission désignée. `resolve_recipients` en est exclu — l'assistant ne
notifie personne, et savoir qui serait joint ne lui sert à rien.
"""

from typing import Any

from shared.mapped_dto import ActionKind

# Lectures MCP ouvertes à l'assistant. Liste blanche, comme pour l'enquêteur :
# un outil ajouté au serveur ne doit pas se retrouver dans sa fenêtre de
# contexte par simple oubli.
ASSISTANT_READ_TOOLS: frozenset[str] = frozenset(
    {
        "list_tms",
        "get_tms_time",
        "get_active_missions",
        "get_mission_snapshot",
        "get_mission_events",
        "get_position_history",
        "get_route",
        "get_driver",
        "get_vehicle",
    }
)


def _action(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Le motif est requis partout : c'est ce qu'un auditeur relira, et la seule
# trace de *pourquoi* une alerte a été close alors que sa condition tenait
# peut-être encore.
_REASON = {
    "type": "string",
    "description": "Ce que l'opérateur a demandé, en une phrase. Conservé pour l'audit.",
}

ACTION_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    ActionKind.CLOSE_ALERT.value: _action(
        ActionKind.CLOSE_ALERT.value,
        "Clôt une alerte ouverte, à la demande explicite de l'opérateur. "
        "La clôture est datée sur l'heure du TMS et journalisée.",
        {
            "alert_id": {"type": "string", "description": "Identifiant de l'alerte à clore."},
            "reason": _REASON,
        },
        ["alert_id", "reason"],
    ),
    ActionKind.CALL_DRIVER.value: _action(
        ActionKind.CALL_DRIVER.value,
        "Appelle le chauffeur d'une mission. Les quotas d'appel s'appliquent et "
        "un refus est rendu comme résultat : ne pas réessayer.",
        {
            "tms_id": {"type": "string", "description": "TMS de la mission."},
            "mission_id": {"type": "string", "description": "Mission concernée."},
            "reason": _REASON,
        },
        ["tms_id", "mission_id", "reason"],
    ),
    ActionKind.LOG_DECISION.value: _action(
        ActionKind.LOG_DECISION.value,
        "Consigne une décision d'exploitation dans le journal, sans rien changer "
        "d'autre. À employer quand la décision est de ne rien faire.",
        {
            "tms_id": {"type": "string", "description": "TMS concerné, si la décision en vise un."},
            "mission_id": {"type": "string", "description": "Mission concernée, le cas échéant."},
            "reason": _REASON,
        },
        ["reason"],
    ),
}
