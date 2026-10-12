"""Réduction des résultats d'outils avant de les remettre au modèle.

Un instantané de mission complet pèse plusieurs milliers de caractères, dont
l'essentiel est constitué de longs tableaux : toute la chronologie, tous les
points de l'itinéraire, tout l'historique de positions. La fenêtre de contexte
ne les supporte pas, et la troncature par nombre de caractères — notre première
approche — s'est révélée **nuisible** :

> Mesuré en conditions réelles, un modèle a répondu « le snapshot est tronqué
> avant le bloc `deviation` » et n'a pas pu donner l'écart à l'itinéraire. La
> troncature avait coupé exactement le champ qui portait la réponse, parce
> qu'il arrivait après la chronologie dans la sérialisation.

D'où une réduction **par le sens** et non par la longueur : on garde les champs
sur lesquels une décision se prend, on résume les tableaux, et on **annonce**
ce qui a été résumé. Un modèle qui lit `events_omitted: 23` sait qu'il peut
demander la chronologie ; un modèle qui lit un JSON coupé au milieu d'un mot ne
sait rien.

Ces réducteurs valent pour l'enquêteur comme pour l'assistant : ce que l'un
doit pouvoir lire, l'autre aussi.
"""

from typing import Any

# Derniers événements conservés dans un instantané. Les plus récents : une
# chronologie se lit par la fin, et c'est le dernier événement qui datera une
# immobilisation.
SNAPSHOT_EVENT_COUNT = 5

# Événements conservés par `get_mission_events`, l'outil que le modèle appelle
# précisément pour lire la chronologie. Plus large, donc.
EVENT_LIST_COUNT = 12

# Positions conservées : les extrémités suffisent à juger d'un déplacement, et
# la trace complète n'apporte rien à un raisonnement.
POSITION_SAMPLE = 3


def reduce_payload(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Réduit la charge utile d'un outil, si un réducteur existe pour lui.

    Sans réducteur, la charge passe inchangée : un outil inconnu ne doit pas
    être amputé au hasard.
    """
    if payload.get("ok") is False:
        # Une erreur est courte et doit arriver intacte : c'est elle qui apprend
        # au modèle comment se corriger.
        return payload
    reducer = _REDUCERS.get(tool)
    return reducer(payload) if reducer else payload


def _reduce_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Garde les champs de décision de l'instantané, résume le reste.

    **L'instantané réduit reste un instantané valide.** C'est la contrainte qui
    gouverne cette fonction : le même objet est lu par le modèle *et* par notre
    code — les faits déterministes, la réévaluation des règles, la résolution
    du chauffeur avant un appel. Les contrats pivot interdisant tout champ
    inattendu, une clé d'annotation glissée dans `snapshot` le rendait
    invalide, et l'enquête se poursuivait « sur les seuls faits », sans
    instantané du tout.

    Ce qui est retiré est donc annoncé **à côté** de l'instantané, dans
    l'enveloppe de la réponse : le modèle le lit dans la même charge utile, et
    la validation du contrat n'en souffre pas.
    """
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        return payload

    reduced = dict(snapshot)
    omissions: dict[str, Any] = {}

    events = snapshot.get("events")
    if isinstance(events, list):
        kept = events[-SNAPSHOT_EVENT_COUNT:]
        reduced["events"] = [_reduce_event(event) for event in kept]
        if len(events) > len(kept):
            # Annoncé, et non silencieux : le modèle doit savoir qu'il peut
            # demander la chronologie complète s'il en a besoin.
            omissions["events_omitted"] = len(events) - len(kept)
            omissions["events_note"] = (
                f"{len(events) - len(kept)} événement(s) plus anciens non inclus : "
                "appeler get_mission_events pour les lire."
            )

    # `planned_route` pèse à lui seul plus de 11 000 caractères sur une mission
    # réelle — contre 300 pour `deviation`. C'est lui qui faisait tomber le
    # champ utile hors de la troncature.
    route = snapshot.get("planned_route")
    if isinstance(route, dict):
        trimmed, dropped = _trim_route(route)
        reduced["planned_route"] = trimmed
        if dropped:
            omissions["route_waypoints_omitted"] = dropped
            omissions["route_note"] = (
                f"Itinéraire réduit à ses extrémités : {dropped} point(s) "
                "intermédiaire(s) retirés, ils ne portent aucune décision."
            )

    result = {**payload, "snapshot": reduced}
    if omissions:
        result["reduction"] = omissions
    return result


def _trim_route(route: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Réduit un itinéraire à ses extrémités, en restant un itinéraire.

    Les points intermédiaires n'ont jamais servi à un raisonnement — un modèle
    ne juge pas d'un tracé en lisant deux cents couples de coordonnées — mais
    ils dominaient la charge utile. Le champ garde sa forme : une liste de
    positions, et les autres attributs du tracé intacts.
    """
    waypoints = route.get("waypoints")
    if not isinstance(waypoints, list) or len(waypoints) <= 2:
        return route, 0
    return {**route, "waypoints": [waypoints[0], waypoints[-1]]}, len(waypoints) - 2


def _reduce_event(event: Any) -> Any:
    """Laisse l'événement intact.

    Il tient en quelques dizaines de caractères, et en retirer des champs le
    rendrait invalide au regard du contrat pivot — c'est le nombre
    d'événements qui pèse, pas leur contenu. La réduction porte donc sur leur
    nombre, décidé par l'appelant.
    """
    return event


def _reduce_events(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events")
    if not isinstance(events, list):
        return payload
    kept = events[-EVENT_LIST_COUNT:]
    reduced = {**payload, "events": [_reduce_event(event) for event in kept]}
    if len(events) > len(kept):
        reduced["events_omitted"] = len(events) - len(kept)
    return reduced


def _reduce_positions(payload: dict[str, Any]) -> dict[str, Any]:
    positions = payload.get("positions")
    if not isinstance(positions, list) or len(positions) <= POSITION_SAMPLE * 2:
        return payload
    return {
        **payload,
        "positions": [*positions[:POSITION_SAMPLE], *positions[-POSITION_SAMPLE:]],
        "positions_omitted": len(positions) - POSITION_SAMPLE * 2,
        "positions_note": (
            "Seules les premières et dernières positions sont incluses ; "
            "`count` donne le total réellement enregistré."
        ),
    }


def _reduce_route(payload: dict[str, Any]) -> dict[str, Any]:
    route = payload.get("route")
    if not isinstance(route, dict):
        return payload
    trimmed, dropped = _trim_route(route)
    reduced = {**payload, "route": trimmed}
    if dropped:
        reduced["reduction"] = {
            "route_waypoints_omitted": dropped,
            "route_note": (
                f"Itinéraire réduit à ses extrémités : {dropped} point(s) intermédiaire(s) retirés."
            ),
        }
    return reduced


_REDUCERS = {
    "get_mission_snapshot": _reduce_snapshot,
    "get_mission_events": _reduce_events,
    "get_position_history": _reduce_positions,
    "get_route": _reduce_route,
}
