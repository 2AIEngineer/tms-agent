"""Primitives partagées par les contrats pivot : base commune et géométrie.

Isolées ici parce que les événements et le snapshot en dépendent tous les deux,
et qu'un import croisé entre ces deux modules n'aurait pas de sens.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Pivot(BaseModel):
    """Base des contrats pivot : stricte à la construction, immuable ensuite.

    `extra="forbid"` est délibéré : un champ inattendu signale un mapper qui
    invente une donnée que les règles ne sauront pas lire, et doit échouer tôt.
    `frozen=True` garantit qu'un snapshot observé ne peut pas être modifié en
    cours d'évaluation — une règle ne doit jamais changer ce qu'elle observe.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Position(Pivot):
    """Point GPS daté. `recorded_at` est en heure TMS."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading: float | None = None
    speed_kmh: float | None = None
    recorded_at: datetime | None = None


class Place(Pivot):
    """Lieu nommé : point de chargement ou de livraison."""

    label: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
