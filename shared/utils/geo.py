"""Calculs géographiques partagés.

Mutualisés parce que deux services en dépendent : le serveur MCP calcule la
distance d'un événement à son étape prévue, et le moteur de règles évalue les
opérateurs géospatiaux. Les deux doivent donner exactement le même résultat,
sinon une alerte afficherait une distance que la règle n'a pas mesurée.
"""

import math

# Rayon moyen de la Terre (WGS 84), en kilomètres.
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique entre deux points, en kilomètres.

    Haversine et non une distance euclidienne : sur des trajets de plusieurs
    centaines de kilomètres, l'écart entre les deux dépasse largement les
    seuils que les règles manipulent, qui se comptent en kilomètres.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
