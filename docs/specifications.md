# Spécification — Orchestrateur de suivi des missions

Oct 6, 2026 · @Saouadogo Salifou

## 1. Contexte

Le suivi des missions de transport repose aujourd'hui sur des agents de suivi d'exploitation qui surveillent chaque camion à la main dans le TMS. le System automatise cette surveillance avec un agent IA, sans modifier le TMS.

**Le métier.** Une mission suit un cycle de vie fixe : déplacement vers le point de chargement, chargement, trajet, déchargement. L'agent de suivi compare en continu la position GPS du camion à l'itinéraire planifié. Il repère les dérives, les retards, les immobilisations et les incidents. Il prévient ensuite les bons interlocuteurs selon la gravité : chauffeur, agent de suivi, superviseur, manager, direction.

**Le problème.** Ce travail est répétitif, continu et difficile à tenir sur un grand nombre de missions simultanées. Une anomalie vue trop tard coûte un retard de livraison ou un client mécontent. Chaque TMS expose aussi ses données différemment, ce qui rend toute automatisation coûteuse à intégrer.

**Le TMS.** Le TMS est un système tiers que nous ne contrôlons pas. Quatre contraintes en découlent :

1. **Lecture seule** : le System ne modifie jamais le TMS. Son API n'autorise que des requetes `GET`.
2. **Pull uniquement** : le TMS ne pousse rien (ni webhook, ni événement). le System l'interroge à son rythme.
3. **Accès via le serveur MCP** : l'agent n'appelle jamais l'API du TMS directement.
4. **Données de l'agent chez l'agent** : alertes, notifications, journal et appels chauffeur sont stockés dans la base de le System, jamais dans le TMS.

**État actuel.** Les vraies règles métier seront formalisées lors d'entretiens avec les agents de suivi. En attendant, un POC tourne sur un TMS simulé (API FastAPI, simulateur de missions, interface web) pour valider l'architecture, la boucle agentique et le découplage entre l'agent, le TMS et les règles.

## 2. Objectifs et périmètre

le System doit surveiller en temps réel les missions de n'importe quel TMS connecté, détecter les anomalies à partir de règles métier explicites, notifier les bons niveaux de management et expliquer chaque alerte. Il vise à augmenter la productivité des agents de suivi, voire à absorber une partie de leur charge.

**Objectifs**** ****de ****l****'****a****g****e****nt**** ****I****A**

1. **Surveillance continue** de toutes les missions actives, sans intervention humaine.
2. **Détection déterministe et auditable** : une alerte naît toujours d'une règle métier YAML versionnée, jamais d'une décision brute du LLM.
3. **Escalade selon la gravité** : chaque règle définit les rôles, les niveaux de management (M+0 à M+n) et les canaux à notifier.
4. **Enquête et explication** : le LLM analyse chaque nouvelle alerte et joint une évaluation structurée (cause probable, risque, actions recommandées, preuves).
5. **Assistant opérateur** : répondre en langage naturel aux questions sur les missions et les alertes.
6. **Indépendance vis-à-vis du TMS** : brancher un nouveau TMS revient à écrire un connecteur. L'agent découvre les tools dynamiquement et n'a aucun code spécifique au TMS.
7. **Règles aux mains du métier** : lisibles, modifiables et testables par l'équipe métier, rechargées à chaud.
8. **Confidentialité** : LLM local par défaut, aucune donnée ne sort de l'infrastructure sans autorisation explicite.


## 3. Acteurs et cas d'usage

Sept acteurs interagissent avec le System : cinq destinataires d'alertes, alignés sur les niveaux de management, plus l'équipe métier et l'équipe technique.

| Acteur | Rôle TMS | Niveau | Canal typique | Ce qu'il attend du système |
| --- | --- | --- | --- | --- |
| Chauffeur | `driver` | M+0 | Appel | Être contacté quand son camion dérive ou s'arrête anormalement |
| Agent de suivi | `ops_agent` | M+1 | In-app | Recevoir chaque alerte avec son analyse, interroger l'assistant, clore les alertes |
| Superviseur | `ops_supervisor` | M+2 | SMS | Être prévenu des anomalies graves (dérive, immobilisation, incident) |
| Manager d'exploitation | `ops_manager` | M+3 | Appel | Être prévenu des incidents critiques et des retards majeurs |
| Direction | `director` | M+4 | E-mail | Être informée des incidents critiques de sécurité |
| Équipe métier | — | — | Fichiers YAML | Écrire, tester et faire évoluer les règles sans développeur |
| Équipe technique | — | — | CLI, logs | Brancher un TMS, déployer, exploiter, superviser |

Les destinataires ne sont pas codés en dur : le serveur MCP les résout dans le TMS à chaque notification. Le rôle `driver` donne le chauffeur de la mission ; les autres rôles donnent les utilisateurs qui portent ce rôle.

**Cas d'usage principaux**

1. **Détecter et notifier** : la boucle détecte une dérive de plus de 5 km depuis plus de 20 min. Elle crée l'alerte, appelle le chauffeur, notifie l'agent de suivi en in-app et le superviseur par SMS.
2. **Expliquer une alerte** : le LLM enquête en arrière-plan (instantané de mission, événements, positions, évaluation des règles) et joint son évaluation à l'alerte.
3. **Clore automatiquement** : quand la condition disparaît (le camion revient sur l'itinéraire) ou que la mission se termine, l'alerte d'état est close.
4. **Interroger l'assistant** : « Quelles missions sont à risque ? », « Pourquoi la mission MSN-2 est-elle en alerte ? ». L'assistant répond en s'appuyant sur les tools MCP et garde le fil de la conversation.
5. **Produire un rapport de situation** : synthèse des missions actives, des alertes ouvertes et des actions menées.
6. **Faire évoluer une règle** : l'équipe métier modifie un seuil, rejoue les scénarios, puis dépose le fichier. La règle est active au cycle suivant, sans redémarrage.
7. **Brancher un nouveau TMS** : l'équipe technique écrit un connecteur vers le contrat pivot. Ni l'agent ni les règles ne changent.


## 4. Spécification fonctionnelle

Les règles décident, le LLM explique. La détection et la notification ne dépendent jamais du LLM : sans lui (panne, quota, pas de clé), l'agent continue d'alerter selon les règles, seule l'analyse est sautée.

### 4.1 Surveillance continue

- **F1.** L'agent exécute un cycle toutes les `poll_interval_s` secondes (10 s par défaut).
- **F2.** Chaque cycle lit les missions en cours, puis l'instantané complet de chacune (mission, événements, véhicule, écart, ETA, chauffeur) en parallèle, avec au plus `mission_concurrency` appels simultanés (6 par défaut).
- **F3.** L'heure de référence est l'heure du TMS (`tms_time`), jamais l'horloge locale. Le système supporte ainsi le temps accéléré du simulateur et la remise à zéro de son horloge.
- **F4.** Une mission illisible ou un instantané partiel est signalé dans le bilan du cycle, sans arrêter le cycle.
- **F5.** Si le TMS ou le serveur MCP est injoignable, le cycle est signalé en échec, l'agent attend avec un backoff exponentiel et se reconnecte automatiquement.

### 4.2 Évaluation des règles

- **F6.** Les règles **d'état** sont évaluées à chaque cycle sur l'instantané de chaque mission (ex. dérive, retard ETA, immobilisation).
- **F7.** Les règles **d'événement** sont évaluées une seule fois par événement nouveau (ex. message chauffeur critique). Un événement n'est marqué traité qu'une fois ses alertes enregistrées ; un échec est repris au cycle suivant.
- **F8.** Les règles sont rechargées à chaud quand un fichier change. Une règle invalide est signalée et l'ancien jeu reste actif.
- **F9.** Les déclenchements sont traités par sévérité décroissante (`critical` → `low`).

### 4.3 Alertes

- **F10.** Chaque déclenchement `emit_alert` crée une alerte avec la règle et sa version, la sévérité, la catégorie, le titre et le message rendus, et les données observées (champs `observe` de la règle).
- **F11.** **Pas de doublon** : le serveur MCP applique atomiquement une clé de déduplication et un cooldown, en temps TMS. Une alerte supprimée est comptée dans le bilan avec sa raison.
- **F12.** Les actions `log_only` et `create_report` sont journalisées sans alerte ni notification.
- **F13.** **Clôture automatique** (`auto_resolve`, activée par défaut) d'une alerte d'état quand sa règle n'est plus vérifiée, ou quand sa mission n'est plus en cours.
- **F14.** **Pas de faux « résolu »** : une alerte d'état n'est close que sur un instantané complet. Une alerte d'événement (incident) n'est jamais close automatiquement tant que la mission est active.

### 4.4 Notifications

- **F15.** Une nouvelle alerte déclenche les notifications du bloc `notify` de sa règle : rôle (ou utilisateur), niveau de management, canal (`in_app`, `sms`, `email`, `call`).
- **F16.** Les destinataires sont résolus dans le TMS au moment de l'envoi. L'adresse dépend du canal : téléphone pour SMS et appel, e-mail pour e-mail, identifiant pour in-app.
- **F17.** Chaque notification est enregistrée avec son statut (`delivered` ou `failed`). Un échec ne recrée pas l'alerte : il est journalisé pour reprise humaine.
- **F18.** Une alerte supprimée par déduplication ne renotifie personne.

### 4.5 Enquêtes du LLM

- **F19.** Chaque nouvelle alerte de sévérité ≥ `investigate_min_severity` (`medium` par défaut) est confiée à l'enquêteur, au plus 4 par cycle, les plus graves d'abord.
- **F20.** Les enquêtes tournent en arrière-plan (2 en parallèle, 8 en attente au plus) : la détection du cycle suivant n'attend jamais le LLM.
- **F21.** L'enquêteur reçoit l'alerte, ses notifications et des faits clés calculés de façon déterministe. Il peut lire le TMS, l'historique de l'agent, évaluer les règles sur la mission et appeler le chauffeur sous garde-fous.
- **F22.** L'enquête se termine par une évaluation structurée (`submit_assessment`) jointe à l'alerte : synthèse, situation, cause probable, niveau de risque, impact client, actions recommandées, preuves, appel chauffeur, suspicion de fausse alerte, confiance.
 
### 4.6 Assistant opérateur

- **F24.** L'assistant répond en langage naturel (`ask` pour une question, `chat` pour une conversation, `report` pour un rapport de situation).
- **F25.** Chaque conversation garde son historique en base, y compris après un redémarrage.
- **F26.** Sur demande explicite, l'assistant peut clore une alerte, appeler un chauffeur ou journaliser une décision. Il n'émet jamais d'alerte ni de notification.

### 4.7 Traçabilité

- **F27.** Toute action de l'agent est journalisée (alertes, notifications, clôtures, enquêtes, appels, échecs) avec la mission et l'alerte concernées.

## 5. Moteur de règles déclaratif

Le moteur  est déterministe, sans LLM ni dépendance MCP. Il reçoit un contexte (mission, véhicule, écart, ETA, chauffeur, événement) et l'heure TMS, et renvoie les règles déclenchées avec leurs données observées, leur message rendu et leurs destinataires.

### 5.1 Format d'une règle

Une règle est un fichier YAML, validé strictement au chargement (Pydantic). Exemple :

```yaml
id: deviation_persistent
version: 2
enabled: true
severity: high                # low | medium | high | critical
category: route_deviation
author: équipe exploitation
description: >
  Alerte si le véhicule s'écarte de plus de 5 km de l'itinéraire pendant plus de 20 minutes.
when:
  all:                        # all (ET) | any (OU) | not, imbricables
    - { field: mission.status, operator: equals, value: in_progress }
    - { field: deviation.current_offset_km, operator: greater_than, value: 5 }
    - { field: deviation.duration_minutes, operator: greater_than, value: 20 }
observe:                      # champs supplémentaires copiés dans l'alerte
  - deviation.vehicle_position
then:
  action: emit_alert          # emit_alert | log_only | create_report
  alert:
    title: "Dérive d'itinéraire"
    message: >
      Le véhicule {{ vehicle.plate }} de la mission {{ mission.reference }}
      s'est écarté de {{ deviation.current_offset_km }} km.
    deduplication_key: "deviation:{{ mission.id }}"
    cooldown_minutes: 30
  notify:
    - { role: driver, management_level: M+0, channel: call }
    - { role: ops_agent, management_level: M+1, channel: in_app }
    - { role: ops_supervisor, management_level: M+2, channel: sms }
```

Les gabarits `{{ champ }}` sont de simples substitutions de chemins, sans code exécutable. Une règle dont une condition porte sur `event.*` est une **règle d'événement** ; les autres sont des **règles d'état**.

### 6.2 Opérateurs

| Famille | Opérateurs |
| --- | --- |
| Comparaison | `equals`, `not_equals`, `greater_than`, `greater_or_equal`, `less_than`, `less_or_equal`, `between` |
| Appartenance | `in`, `not_in` |
| Temporel | `older_than_minutes`, `newer_than_minutes`, `duration_greater_than_minutes` |
| Géospatial | `distance_greater_than_km` (haversine), `inside_polygon`, `outside_polygon` |
| Logique | `all`, `any`, `not` |

Chaque opérateur valide le type de sa valeur au chargement. Un champ absent du contexte rend la condition fausse, sans erreur.

