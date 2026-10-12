"""Traduction du modèle propre à un TMS vers le contrat pivot.

    JSON TMS  +  dialecte  ->  PivotMapper  ->  MissionSnapshot / MissionEvent

Le mapper ne connaît aucun nom de champ : il lit les correspondances déclarées
dans le dialecte du TMS. Brancher un nouveau TMS consiste donc à écrire son
dialecte, et à n'écrire du code que pour ce qu'une configuration ne peut pas
exprimer : un calcul, une réduction de structure, un transport exotique.
"""

from .pivot import PivotMapper, as_float

__all__ = ["PivotMapper", "as_float"]
