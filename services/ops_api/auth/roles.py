"""Rôles et permissions de l'API d'exploitation.

Les rôles sont **ceux du TMS** (`ops_agent`, `ops_supervisor`, …) : un agent de
suivi n'a pas à se voir attribuer un rôle supplémentaire dans notre système
pour y faire ce qu'il fait déjà dans le sien. Ils arrivent dans le jeton Entra,
que l'administrateur du client alimente depuis son annuaire.

Deux permissions seulement, et c'est délibéré : la granularité par endpoint
produit des matrices que personne ne relit.

  - **lire** — alertes, missions, enquêtes, poser une question à l'assistant.
    Ne change rien ;
  - **agir** — clore une alerte, faire appeler un chauffeur, consigner une
    décision. Chacune est journalisée avec son demandeur (F27).

Un rôle inconnu ne reçoit aucune permission. C'est le comportement voulu : un
annuaire mal configuré doit refuser l'accès, non l'accorder par défaut.
"""

from enum import StrEnum


class Permission(StrEnum):
    READ = "read"
    ACT = "act"


# Rôles alignés sur ceux du TMS, et ce que chacun peut faire. Le chauffeur
# n'y figure pas : il n'ouvre pas cette interface.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "ops_agent": frozenset({Permission.READ, Permission.ACT}),
    "ops_supervisor": frozenset({Permission.READ, Permission.ACT}),
    "transport_manager": frozenset({Permission.READ, Permission.ACT}),
    "operations_director": frozenset({Permission.READ}),
    "general_management": frozenset({Permission.READ}),
    # Lecture seule explicite : utile pour un tableau mural ou un auditeur.
    "ops_viewer": frozenset({Permission.READ}),
}


def permissions_for(roles: tuple[str, ...]) -> frozenset[Permission]:
    """Union des permissions des rôles portés par le jeton."""
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)
