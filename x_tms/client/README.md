# Client — suivi des missions en temps réel

Interface web pour visualiser les missions du TMS : carte, itinéraire planifié, trace GPS réelle, dérives, ETA et chronologie des événements.

Stack : Vite + React 19 + TypeScript, shadcn/ui (Base UI) + Tailwind 4, MapLibre GL JS + tuiles OpenStreetMap, TanStack Query. Le tracé d'itinéraire est décodé depuis la polyline Google de l'API et affiché en couche GeoJSON.

## Démarrage

```bash
cd client && npm install && npm run dev
# -> http://localhost:5173
```

## Fonctionnement

Le client respecte les contraintes d'intégration du TMS (voir `docs/GOAL.md`) :

- **Lecture seule** : uniquement des `GET` sur l'API.
- **Pull uniquement** : chaque ressource est interrogée toutes les 2 s (`VITE_POLL_MS`). Le polling s'arrête pour les missions terminées ou annulées.
- **Trace GPS incrémentale** : l'historique est chargé une fois, puis seules les nouvelles positions sont récupérées (`/vehicles/{id}/positions?mission_id=…&since=<dernière position>`).
- **Heure du TMS** : les durées relatives (âge du dernier événement, « vu il y a… ») sont calculées avec l'heure renvoyée par `/v1/health`. Elles restent justes quand la simulation accélère le temps.

## Écran

| Zone | Contenu |
|---|---|
| En-tête | Heure du TMS, vitesse de simulation, état de la connexion à l'API |
| Liste (gauche) | Missions actives ou toutes. Pour chacune : ETA avec retard coloré et écart à l'itinéraire s'il dépasse 0,5 km |
| Carte (centre) | Voir le détail ci-dessous |
| Panneau (droite) | Voir le détail ci-dessous |

Sur la carte :

- en vue flotte, tous les véhicules en mission, orientés selon leur cap et colorés selon leur statut (en mouvement, à l'arrêt, hors ligne) ;
- pour une mission sélectionnée :
  - l'itinéraire planifié et la trace réelle (approche, sur l'itinéraire, hors itinéraire en rouge) ;
  - un trait pointillé entre le véhicule et l'itinéraire pendant une dérive ;
  - les événements cliquables et les points A et B ;
- les boutons « Suivre le véhicule » et « Recadrer ».

Dans le panneau de la mission :

- une barre d'étapes (approche, chargement, trajet, livraison) et la progression en km ;
- quatre indicateurs : ETA et retard, écart à l'itinéraire, véhicule, âge du dernier événement ;
- le chauffeur, le planning prévu et réel, et la chronologie des événements.

La mission sélectionnée est conservée dans l'URL (`?mission=msn_…`) : le lien se partage tel quel.

Code couleur des indicateurs :

- **Retard d'ETA** : orange au-delà de 10 min, rouge au-delà de 45 min.
- **Écart à l'itinéraire** : orange au-delà de 0,5 km, rouge à partir de 5 km.
- **Dernier événement** : orange au-delà d'1 h, rouge au-delà de 2 h.

Ce code couleur aide seulement la lecture. Les vraies règles d'alerte vivent dans le moteur de règles du projet orchestrateur.

## Structure

```
src/
├── lib/api.ts             client HTTP typé (GET, pagination, erreurs RFC 7807)
├── lib/format.ts          libellés FR, formats de dates et de durées, seuils de couleur
├── lib/geo.ts             distance d'un point au tracé, emprise
├── hooks/useTms.ts        requêtes avec polling, trace GPS incrémentale
├── components/
│   ├── AppHeader.tsx
│   ├── MissionList.tsx
│   ├── MissionMap.tsx     carte MapLibre (couches GeoJSON, marqueurs)
│   ├── MissionPanel.tsx
│   ├── FleetPanel.tsx
│   └── ui/                composants shadcn/ui
└── App.tsx
```

## Scripts

| Commande | Rôle |
|---|---|
| `npm run dev` | Serveur de développement |
| `npm run build` | Vérification TypeScript + build de production (`dist/`) |
| `npm run lint` | oxlint |
