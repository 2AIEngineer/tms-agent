# TMS API (un faux TMS)

API REST qui simule un TMS réel (contrat défini dans [docs/GOAL.md](../docs/GOAL.md), Document 1), et simulateur qui fait vivre des missions de transport.
L'API expose des **faits** (positions, événements, écart à l'itinéraire, ETA) et ne prend **aucune décision métier** : la couche MCP, l'agent IA et le moteur de règles sont dans un projet séparé.

## Principe d'intégration

Ce TMS représente un système **que nous ne contrôlons pas** : nous l'intégrons pour en consommer les ressources.

- **Lecture seule** : l'API n'expose que des `GET`. L'agent IA ne fait aucune mutation.
- **Pull uniquement** : le TMS ne pousse rien vers l'agent (ni webhook, ni événement). C'est l'agent qui appelle l'API, toujours via le serveur MCP. Pour suivre les changements, il fait du polling avec `updated_since` et `since`.
- **Données de l'agent hors du TMS** : alertes, notifications, journaux et appels chauffeur sont stockés dans la base de l'agent IA, pas ici.
- **Seul écrivain : le simulateur.** `tms start-missions` joue le rôle des systèmes internes du TMS (boîtiers télématiques, appli chauffeur). Il écrit directement en base, sans passer par l'API.

Stack : FastAPI, SQLAlchemy 2 (SQLite par défaut), Typer, uv.

## Démarrage rapide

```bash
cd server
uv sync

# Terminal 1 : l'API (doc interactive sur http://127.0.0.1:8000/v1/docs)
uv run tms serve
```

## Commandes

| Commande | Rôle |
|---|---|
| `tms serve [--host --port --reload]` | Démarre l'API |
| `tms start-missions [options]` | Crée des missions et les fait vivre jusqu'à leur terme |
| `tms scenarios` | Liste les scénarios disponibles |
| `tms reset [-y]` | Supprime missions, événements, positions et itinéraires (conserve utilisateurs/véhicules) |
| `tms init-db` | Crée les tables et charge les données de test (fait automatiquement au démarrage) |

Options de `start-missions` :

| Option | Défaut | Effet |
|---|---|---|
| `-s, --scenario` | `nominal`, `deviation`, `immobilization` | Scénario(s) à lancer (répétable). `incident` est disponible en plus |
| `--speed` | `1` | Accélération du temps (`60` = 1 min simulée par seconde) |
| `--tick-seconds` | `30` | Intervalle simulé entre deux positions GPS |
| `--skip-minutes N` | `0` | Joue instantanément les N premières minutes (ex. `--skip-minutes 60` pour arriver directement sur la dérive) |
| `--fast-forward` | non | Joue toute la simulation instantanément (historique complet, utile pour le frontend) |
| `--routing static\|osrm` | `static` | Itinéraires hors-ligne approximatifs, ou routes réelles via le serveur public OSRM (repli sur `static` si indisponible) |
| `--reset` | non | Supprime les missions existantes avant de démarrer |
| `--seed` | aléatoire | Graine pour une simulation reproductible |

Ctrl+C (ou `kill`) annule proprement les missions non terminées.

## Scénarios

Chronologie commune : approche du point de chargement (10 min), `arrived_pickup`, chargement (`loading_started` à +5 min, `loading_completed` à +30 min), `departed_pickup` (+32 min), trajet, `arrived_delivery`, `unloading_started`, `unloading_completed`. Une position GPS toutes les 30 s.

| Scénario | Trajet | Anomalie | Règles visées |
|---|---|---|---|
| `nominal` | Casablanca → Rabat (veh_201, drv_77) | aucune | — |
| `deviation` | Casablanca → Marrakech (veh_202, drv_78) | à T+45 min, décalage progressif de 6 à 8 km pendant 30 min | `deviation_persistent` |
| `immobilization` | Tanger → Fès (veh_203, drv_79) | à T+1 h, véhicule `stopped` et aucun événement pendant 3 h | `immobilization_anomaly`, `eta_delay` |
| `incident` *(optionnel)* | Casablanca → El Jadida (veh_204, drv_80) | à T+55 min, `driver_message` de sévérité `critical`, arrêt de 40 min | `critical_incident` |

## Temps simulé

Avec `--speed`, le temps du TMS est accéléré. L'API et le simulateur partagent la même horloge, stockée en base. Toutes les durées exposées (âge du dernier événement, durée d'une dérive, ETA) sont donc cohérentes avec le temps simulé. L'heure courante du TMS est donnée par `GET /v1/health` (`time`). Une fois la simulation terminée, l'horloge repart à vitesse réelle à partir de l'heure simulée atteinte, et ne recule jamais. `tms reset` la remet à l'heure réelle.

## Endpoints

Tous préfixés par `/v1`. Spécification OpenAPI 3.1 : `/v1/openapi.json`. Documentation : `/v1/docs`.

Toutes les routes sont en `GET`.

| Endpoint | Description |
|---|---|
| `/missions` | Liste paginée. Filtres `status` (répétable), `vehicle_id`, `driver_id`, `updated_since` |
| `/missions/{id}` | Détail, avec `events[]` |
| `/missions/{id}/events` | Événements. Filtres `type` (répétable), `since` |
| `/missions/{id}/route` | Itinéraire planifié (waypoints + polyline Google) |
| `/missions/{id}/deviation` | Écart courant à l'itinéraire, durée, sévérité |
| `/missions/{id}/eta` | ETA recalculée, retard en minutes |
| `/vehicles` | Liste. Filtres `status`, `fleet_id` |
| `/vehicles/{id}` | Détail, dont `current_mission_id` |
| `/vehicles/{id}/position` | Dernière position connue |
| `/vehicles/{id}/positions` | Historique. Filtres `since`, `until`, `mission_id` (jusqu'à 2000 points par page) |
| `/drivers/{id}` | Chauffeur, avec statut `available` / `on_mission` |
| `/users` | Utilisateurs. Filtres `role`, `management_level` |
| `/users/{id}` | Détail d'un utilisateur |
| `/roles/{role}/users` | Utilisateurs d'un rôle (permet à l'agent de résoudre les destinataires d'une notification) |
| `/health` | Statut, heure du TMS, état de la simulation |

### Conventions

- **Horodatages** ISO 8601 UTC (`2026-10-01T12:00:00Z`).
- **Pagination** : `?limit=` (défaut 50) et `?cursor=` ; réponse `{"data": [...], "pagination": {"cursor": "...", "has_more": true}}`.
- **Erreurs** RFC 7807 (`application/problem+json`) : `{"type", "title", "status", "detail", "instance"}`. Les erreurs de validation (422) ajoutent un champ `errors`.

### Contrat

Le contrat de référence est le Document 1 de [docs/GOAL.md](../docs/GOAL.md), à jour avec la contrainte de lecture seule. Les alertes, notifications, journaux et appels chauffeur relèvent du projet orchestrateur (Documents 2 et 3).

### Sémantique des faits calculés

- **Mission** : les événements (source `driver_app` ou `tms`) mettent à jour la mission :
  - `arrived_pickup` renseigne `actual_pickup_at` ;
  - `arrived_delivery` renseigne `actual_delivery_at` ;
  - `unloading_completed` passe la mission en `completed`.
- **Deviation** : distance entre la dernière position et le tracé planifié, calculée à partir de `departed_pickup` (avant, les champs valent `null`).
  - `current_offset_since` : moment où l'écart a dépassé 0,5 km.
  - Sévérité : `low` en dessous de 2 km, `medium` de 2 à 10 km, `high` à partir de 10 km.
- **ETA** :
  - avant le départ : `max(arrivée planifiée, maintenant + durée du trajet)` ;
  - en route : `maintenant + distance restante / vitesse moyenne planifiée`.
- **Véhicule** : `moving` (3 km/h ou plus) ou `stopped` selon la vitesse, `idle` hors mission, `offline` après 15 min sans position.

## Données de test

| Id | Nom | Rôle | Niveau |
|---|---|---|---|
| drv_77 … drv_80 | Chauffeurs | `driver` | M+0 |
| usr_agent_12 | Salma R. | `ops_agent` | M+1 |
| usr_sup_04 | Yassine B. | `ops_supervisor` | M+2 |
| usr_mgr_02 | Nadia K. | `ops_manager` | M+3 |
| usr_dir_01 | Omar F. | `director` | M+4 |

Véhicules : `veh_201` à `veh_204` (flottes `flt_casa`, `flt_nord`).

## Configuration

Variables d'environnement (ou fichier `.env`) :

| Variable | Défaut |
|---|---|
| `TMS_DATABASE_URL` | `sqlite:///server/data/tms.db` |
| `TMS_CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` (Vite) |
| `TMS_ON_ROUTE_TOLERANCE_KM` | `0.5` |
| `TMS_OFFLINE_AFTER_MINUTES` | `15` |

## Tests

```bash
uv run pytest
```

Les tests rejouent les scénarios et vérifient les faits attendus par les règles du Document 3 :

- dérive de 6 à 8 km pendant plus de 20 min ;
- immobilisation de plus de 120 min sans événement ;
- retard d'ETA de plus de 45 min.

> Si un `PYTHONPATH` global (ROS, par exemple) injecte des plugins pytest, lancer `env -u PYTHONPATH uv run pytest`.
