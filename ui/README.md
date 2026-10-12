# UI — assistant opérateur

Interface de l'assistant de suivi des missions : questions, rapports et actions
validées par l'opérateur. Elle ne parle qu'à l'**API d'exploitation**
(`services/ops_api`, port 8020) et n'a aucun lien avec le client du TMS simulé
(`x_tms/client`). Pas de carte temps réel : ce n'est pas son rôle.

Stack : Vite · React 19 · TypeScript · Tailwind 4 · shadcn/ui (base-ui) · TanStack Query.

```bash
cd ui
npm install
npm run dev        # http://localhost:5180, /api -> http://127.0.0.1:8020
npm run build
```

Pour joindre une autre API : `VITE_API_TARGET` (proxy de développement) ou
`VITE_API_URL` (build), ou les réglages de l'interface (adresse et jeton Entra,
conservés dans le navigateur). En développement, lancer l'API avec
`OPS_API_AUTH_DISABLED=true`.

## Fonctions

- **Assistant** : conversation en streaming (SSE), étapes de consultation du
  TMS visibles (outils, durée, aperçu), Markdown, sources, jetons ; modes
  conversation / question / rapport ; rattachement d'une alerte ou d'une
  mission ; arrêt de la génération ; reprise d'une conversation.
- **Actions** : autorisation d'agir par requête (désactivée si le rôle ne le
  permet pas), carte de validation avec résumé déterministe et compte à rebours,
  valider / refuser, résultat dans le fil.
- **Alertes** : synthèse par sévérité, filtres, détail (enquête, notifications,
  appels, audit), « demander à l'assistant », toasts en direct (SSE).
- **Missions**, **Rapports** planifiés, **Système** (santé, cycles de
  surveillance, file d'enquêtes).
- Palette `Ctrl K`, thème clair/sombre, responsive.
