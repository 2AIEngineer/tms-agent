# Architecture cible --- Orchestrateur de suivi des missions

**Version :** 1.0\
**Date :** 7 octobre 2026\
**Statut :** Proposition d'architecture cible\
**Base :** Spécification technique --- Orchestrateur de suivi des
missions

------------------------------------------------------------------------

## 1. Objet du document

Ce document décrit l'architecture cible proposée pour la mise en œuvre
industrielle de l'orchestrateur de suivi des missions.

L'objectif est de partir des règles métier et du besoin fonctionnel
validés, puis de construire une architecture de production claire,
découplée et maintenable.

Le POC n'est pas considéré comme le plan d'implémentation définitif. Il
sert à réduire les incertitudes techniques et à valider les principaux
choix. L'architecture cible doit cependant être organisée autour du
workflow métier réel.

Le principe directeur est le suivant :

> Le monitoring et la décision métier doivent rester déterministes. L'IA
> intervient ensuite pour investiguer, expliquer, recommander et
> assister les opérateurs.

------------------------------------------------------------------------

# 2. Principes architecturaux

## 2.1 Le TMS reste la source de vérité

Le TMS demeure la source de vérité opérationnelle pour les missions,
leurs états, les positions, les événements et les informations
nécessaires au suivi.

L'orchestrateur :

-   ne modifie pas le TMS ;
-   récupère les informations nécessaires par lecture ;
-   ne maintient pas une copie complète des missions ;
-   utilise le TMS comme référence pour l'état courant.

Le TMS étant dépourvu de mécanisme de webhook dans le contexte actuel,
le monitoring fonctionne par polling périodique.

``` text
TMS
 │
 │ lecture
 ▼
Orchestrateur
```

Le flux inverse n'est pas autorisé :

``` text
Orchestrateur ──X──> modification du TMS
```

------------------------------------------------------------------------

## 2.2 MCP est la frontière d'intégration avec le TMS

Le MCP Server constitue l'unique frontière entre l'orchestrateur et le
TMS.

Le reste du système ne connaît pas :

-   l'URL du TMS ;
-   ses endpoints REST ;
-   son modèle JSON ;
-   ses mécanismes de pagination ;
-   ses codes d'erreur spécifiques ;
-   ses particularités d'authentification.

Ces détails sont encapsulés dans le connecteur TMS.

``` text
                    ┌─────────────┐
                    │     TMS     │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ MCP Server  │
                    │             │
                    │ Connector   │
                    │ Tools       │
                    │ Guardrails  │
                    └──────┬──────┘
                           │
                      MCP protocol
                           │
                           ▼
                    Orchestrateur
```

Le principe est :

> MCP est la frontière TMS, pas le moteur métier de l'orchestrateur.

------------------------------------------------------------------------

## 2.3 Le monitoring n'est pas un agent LLM

Le monitoring doit pouvoir fonctionner sans LLM.

Il est responsable de :

-   récupérer les missions ;
-   récupérer les snapshots ;
-   calculer les données dérivées ;
-   évaluer les règles ;
-   créer les alertes ;
-   gérer la déduplication ;
-   appliquer les cooldowns ;
-   gérer le cycle de vie des alertes ;
-   déclencher les notifications.

``` text
Snapshot
   │
   ▼
Rules Engine
   │
   ▼
Rule Evaluation
   │
   ▼
Alert
   │
   ▼
Notification
```

Aucune décision critique de détection ne doit dépendre d'une
interprétation probabiliste du LLM.

------------------------------------------------------------------------

## 2.4 Le LLM intervient après la détection

Le LLM intervient lorsqu'une situation a déjà été identifiée par une
règle déterministe.

``` text
Règle déterministe
       │
       ▼
     Alerte
       │
       ▼
Investigation IA
       │
       ▼
Explication / cause probable / risque / actions
```

Le LLM peut :

-   analyser le contexte ;
-   rechercher des informations complémentaires ;
-   consulter l'historique ;
-   analyser les événements ;
-   interpréter une situation ;
-   recommander des actions ;
-   produire une synthèse.

Le LLM ne doit pas décider seul :

> « Cette situation doit créer une alerte. »

------------------------------------------------------------------------

## 2.5 Les responsabilités doivent être explicites

Chaque composant doit avoir une responsabilité identifiable.

  Composant              Responsabilité principale
  ---------------------- -----------------------------------------------
  TMS                    Source de vérité opérationnelle
  MCP Server             Accès contrôlé au TMS
  TMS Connector          Traduction TMS → contrat pivot
  Monitoring Engine      Surveillance déterministe
  Rules Engine           Évaluation des règles métier
  Alert Service          Cycle de vie des alertes
  Notification Service   Notification et suivi d'envoi
  Agent Runtime          Orchestration des workflows IA
  Investigator           Investigation d'une alerte
  Assistant              Interaction naturelle avec les opérateurs
  PostgreSQL             État, audit et persistence de l'orchestrateur
  LLM                    Raisonnement et génération

------------------------------------------------------------------------

# 3. Architecture cible globale

``` text
                         ┌─────────────────────┐
                         │         TMS         │
                         │  Source de vérité   │
                         └──────────┬──────────┘
                                    │
                                    │ REST / Connector
                                    ▼
                         ┌─────────────────────┐
                         │     MCP SERVER      │
                         │                     │
                         │ TMS Connector       │
                         │ TMS Semantic Tools  │
                         │ Guardrails          │
                         └──────────┬──────────┘
                                    │
                              MCP Protocol
                                    │
                ┌───────────────────┴───────────────────┐
                │                                       │
                ▼                                       ▼
       ┌────────────────────┐                 ┌────────────────────┐
       │ MONITORING SERVICE │                 │   AGENT SERVICE    │
       │                    │                 │                    │
       │ Scheduler          │                 │ Investigator       │
       │ Snapshot           │                 │ Assistant          │
       │ Derived data       │                 │ LangGraph          │
       │ Rules Engine       │                 │ MCP Client         │
       │ Alert Service      │                 │ Tool orchestration │
       │ Notification       │                 └─────────┬──────────┘
       └──────────┬─────────┘                           │
                  │                                     │
                  └────────────────┬────────────────────┘
                                   │
                                   ▼
                           ┌───────────────┐
                           │  PostgreSQL   │
                           │               │
                           │ monitor       │
                           │ agent         │
                           │ assistant     │
                           └───────┬───────┘
                                   │
                                   ▼
                              ┌─────────┐
                              │   LLM   │
                              │ vLLM    │
                              │ Qwen... │
                              └─────────┘
```

L'API HTTP et l'interface utilisateur constituent une couche applicative
complémentaire au-dessus des services métier.

``` text
                    Frontend / UI
                          │
                          ▼
                   Application API
                          │
             ┌────────────┼────────────┐
             ▼            ▼            ▼
          Alerts       Missions     Assistant
             │            │            │
             └────────────┼────────────┘
                          ▼
                     Application
                       Services
                          │
                          ▼
                      PostgreSQL
```

L'API applicative et MCP ne doivent pas être confondus :

-   l'API sert l'application et les utilisateurs ;
-   MCP expose des capacités spécialisées aux clients agentiques.

------------------------------------------------------------------------

# 4. Découpage des services

## 4.1 MCP Server

### Responsabilité

Le MCP Server est responsable de l'intégration avec le TMS.

Il encapsule :

-   le connecteur TMS ;
-   les appels REST ;
-   la pagination ;
-   les timeouts ;
-   les retries ;
-   les erreurs TMS ;
-   les transformations vers le contrat pivot ;
-   les outils MCP ;
-   les garde-fous d'utilisation.

### Il ne doit pas contenir

Le MCP Server ne doit pas devenir :

-   le moteur de règles ;
-   le moteur de monitoring ;
-   le runtime LangGraph ;
-   le gestionnaire global des alertes ;
-   le backend CRUD de toute l'application.

### Organisation

``` text
mcp_server/
│
├── connectors/
│   ├── base.py
│   └── tms.py
│
├── tools/
│   ├── missions.py
│   ├── tracking.py
│   ├── participants.py
│   └── operations.py
│
└── schemas/
    ├── mission.py
    ├── tracking.py
    └── common.py
```

------------------------------------------------------------------------

# 5. Monitoring Service

Le Monitoring Service constitue le cœur déterministe du produit.

## 5.1 Responsabilités

``` text
Scheduler
   ↓
Mission discovery
   ↓
Snapshot retrieval
   ↓
Normalization
   ↓
Derived fields
   ↓
Rules Engine
   ↓
Alert Service
   ↓
Notification Service
```

Le monitoring doit continuer à fonctionner même si le LLM est
indisponible.

Une indisponibilité du LLM peut empêcher une investigation, mais ne doit
pas empêcher la détection d'une anomalie.

------------------------------------------------------------------------

## 5.2 Découpage du code

``` text
monitoring/
│
├── scheduler.py
│
├── snapshot_service.py
│
├── rules/
│   ├── engine.py
│   ├── models.py
│   └── loader.py
│
├── alerts/
│   ├── service.py
│   ├── models.py
│   └── repository.py
│
└── notifications/
    ├── service.py
    ├── models.py
    └── repository.py
```

------------------------------------------------------------------------

# 6. Agent Service

L'Agent Service héberge les workflows agentiques.

Deux workflows principaux sont séparés.

## 6.1 Investigator

Déclenché par une alerte.

``` text
Alert
 │
 ▼
Investigation Request
 │
 ▼
Investigator
 │
 ├── LLM
 ├── MCP tools
 ├── Mission context
 ├── Historical data
 └── Driver interaction
 │
 ▼
Investigation Result
```

## 6.2 Assistant

Déclenché par une demande opérateur.

``` text
Operator
   │
   ▼
Assistant
   │
   ├── understand request
   ├── select tools
   ├── query TMS
   ├── query history
   └── synthesize
   │
   ▼
Answer
```

Ces deux workflows peuvent partager :

-   MCP Client ;
-   modèles communs ;
-   infrastructure LLM ;
-   mécanismes de tracing ;
-   configuration ;

mais ils doivent rester deux workflows métier distincts.

------------------------------------------------------------------------

# 7. Découpage du code Agent Service

``` text
agents/
│
├── investigator/
│   ├── graph.py
│   ├── prompts.py
│   ├── models.py
│   └── service.py
│
├── assistant/
│   ├── graph.py
│   ├── prompts.py
│   ├── models.py
│   └── service.py
│
└── ...
```

L'objectif est de conserver des composants explicites plutôt que de
construire une hiérarchie excessive d'abstractions génériques.

À éviter :

``` text
BaseAgent
BaseTool
BaseRepository
BasePolicy
BaseResource
BaseService
...
```

si ces abstractions ne correspondent pas à un véritable besoin partagé.

------------------------------------------------------------------------

# 8. Contrats entre composants

Les composants ne doivent pas partager leurs modèles internes.

Ils communiquent via des contrats explicites.

Les principaux contrats sont :

``` text
MissionSnapshot
RuleEvaluation
Alert
NotificationRequest
InvestigationRequest
InvestigationResult
```

------------------------------------------------------------------------

# 9. MissionSnapshot

Le `MissionSnapshot` représente l'état d'une mission à un instant donné
dans le modèle canonique de l'orchestrateur.

``` python
class MissionSnapshot(BaseModel):
    mission_id: str
    status: MissionStatus

    driver: DriverSnapshot | None
    vehicle: VehicleSnapshot | None

    loading: StopSnapshot | None
    unloading: StopSnapshot | None

    planned_route: RouteSnapshot | None
    current_position: PositionSnapshot | None

    planned_times: PlannedTimes
    actual_times: ActualTimes

    events: list[MissionEvent]

    observed_at: datetime
    snapshot_complete: bool
```

Le Monitoring Engine ne doit jamais dépendre directement du JSON brut du
TMS.

``` text
TMS JSON
   ↓
TmsConnector
   ↓
MissionSnapshot
   ↓
Monitoring
```

------------------------------------------------------------------------

# 10. Contrat pivot et connecteur TMS

Le connecteur transforme le modèle spécifique du TMS vers le modèle
canonique.

``` text
                 TMS A
                   │
                   ▼
              Connector A
                   │
                   ▼
             MissionSnapshot
                   ▲
                   │
              Connector B
                   ▲
                   │
                 TMS B
```

Ainsi, remplacer le TMS ne nécessite pas de réécrire :

-   le Monitoring Engine ;
-   les règles ;
-   le système d'alertes ;
-   l'investigator ;
-   l'assistant.

Le connecteur absorbe les différences spécifiques du TMS.

------------------------------------------------------------------------

# 11. RuleEvaluation

Le Rules Engine ne crée pas directement les alertes.

Il produit un résultat d'évaluation.

``` python
class RuleEvaluation(BaseModel):
    rule_id: str
    rule_version: str

    matched: bool

    severity: Severity | None
    category: str | None

    observed_data: dict[str, Any]

    evaluated_at: datetime
```

Workflow :

``` text
MissionSnapshot
      │
      ▼
 Rules Engine
      │
      ▼
RuleEvaluation
      │
      ▼
 Alert Service
```

Cette séparation permet de tester les règles indépendamment de la
persistence et des notifications.

------------------------------------------------------------------------

# 12. Alert

L'alerte représente un événement métier détecté par une règle.

``` python
class Alert(BaseModel):
    id: UUID

    mission_id: str

    rule_id: str
    rule_version: str

    severity: Severity
    category: AlertCategory

    title: str
    message: str

    observed_data: dict[str, Any]

    status: AlertStatus

    detected_at: datetime
    resolved_at: datetime | None
```

La version de règle doit être conservée.

Exemple :

``` text
rule_id      = mission_delay
rule_version = 3
```

Cela permet de reconstruire ultérieurement pourquoi l'alerte a été
créée.

------------------------------------------------------------------------

# 13. Alert Service

Le `Alert Service` est responsable du cycle de vie des alertes.

Il gère :

-   création ;
-   déduplication ;
-   cooldown ;
-   statut ;
-   résolution ;
-   audit ;
-   rattachement à une règle ;
-   conservation des données observées.

Workflow :

``` text
RuleEvaluation
      │
      ▼
 matched ?
   │       │
  NO      YES
   │       │
   ▼       ▼
  END    Deduplication
             │
             ▼
          Cooldown
             │
             ▼
         Create/Update
             │
             ▼
            Alert
```

Le Rules Engine ne connaît pas les détails de persistence.

------------------------------------------------------------------------

# 14. Notification Service

Les notifications sont séparées des alertes.

``` text
Alert
  │
  ▼
Notification Policy
  │
  ▼
Resolve Recipients
  │
  ▼
NotificationRequest
  │
  ▼
Send
  │
  ▼
Notification Status
```

Exemple de contrat :

``` python
class NotificationRequest(BaseModel):
    alert_id: UUID

    recipients: list[Recipient]
    channel: NotificationChannel

    severity: Severity

    message: str
```

La résolution des destinataires peut utiliser le TMS via MCP au moment
de l'envoi.

------------------------------------------------------------------------

# 15. Investigation IA

L'investigation est déclenchée après la création d'une alerte.

``` python
class InvestigationRequest(BaseModel):
    investigation_id: UUID

    alert_id: UUID
    mission_id: str

    alert: Alert

    observed_facts: dict[str, Any]

    requested_at: datetime
```

L'agent reçoit donc un contexte initial contrôlé.

Il ne reçoit pas un accès direct aux tables PostgreSQL.

------------------------------------------------------------------------

# 16. Accès aux informations par l'agent

L'agent enrichit son contexte via les tools MCP.

``` text
Agent
 │
 ├── get_mission()
 ├── get_mission_events()
 ├── get_position_history()
 ├── get_route()
 ├── get_driver()
 └── call_driver()
```

Le principe est :

> L'agent travaille avec des capacités métier, pas avec les tables de la
> base.

À éviter :

``` text
Agent
  ↓
SQL
  ↓
SELECT *
```

Préférer :

``` text
Agent
  ↓
MCP Tool
  ↓
Application capability
  ↓
Data source
```

------------------------------------------------------------------------

# 17. InvestigationResult

La sortie de l'agent doit être structurée.

``` python
class InvestigationResult(BaseModel):
    investigation_id: UUID
    alert_id: UUID

    summary: str
    situation: str
    probable_cause: str | None

    risk: str
    client_impact: str | None

    recommended_actions: list[str]

    evidence: list[Evidence]

    driver_call: DriverCallAssessment | None

    false_positive_suspicion: bool

    confidence: float

    completed_at: datetime
```

L'agent produit donc une analyse exploitable par :

-   l'interface ;
-   l'historique ;
-   les rapports ;
-   l'opérateur ;
-   l'audit.

------------------------------------------------------------------------

# 18. Séparation décision / raisonnement

Il existe deux types de décisions dans le système.

## Décision déterministe

``` text
MissionSnapshot
      │
      ▼
Rules Engine
      │
      ▼
RuleEvaluation
      │
      ▼
Alert
```

Question :

> Une règle métier est-elle satisfaite ?

## Analyse probabiliste

``` text
Alert
  │
  ▼
Investigator
  │
  ▼
InvestigationResult
```

Questions :

-   Pourquoi cela arrive-t-il ?
-   Quelle est la cause probable ?
-   Quel est le risque ?
-   Quel est l'impact ?
-   Quelles actions sont recommandées ?
-   S'agit-il potentiellement d'un faux positif ?

Cette séparation doit rester une contrainte architecturale forte.

------------------------------------------------------------------------

# 19. MCP Tools

Les tools MCP doivent être organisés par capacité métier.

``` text
MCP Server
│
├── Mission
│   ├── get_active_missions
│   ├── get_mission
│   └── get_mission_events
│
├── Tracking
│   ├── get_current_position
│   ├── get_position_history
│   └── get_route
│
├── Participants
│   ├── get_driver
│   ├── get_vehicle
│   └── resolve_recipients
│
└── Operations
    └── call_driver
```

Chaque tool doit avoir :

-   un nom explicite ;
-   une description ;
-   un input schema ;
-   un output schema ;
-   des guardrails ;
-   un timeout ;
-   un contrat d'erreur.

------------------------------------------------------------------------

# 20. Éviter un MCP CRUD générique

Il ne faut pas transformer MCP en façade générique de PostgreSQL.

À éviter :

``` text
get_alerts()
create_alert()
update_alert()
delete_alert()
get_notifications()
create_notification()
...
```

simplement parce qu'une base de données existe derrière.

Les tools doivent exposer des capacités utiles au workflow.

Par exemple :

``` text
get_mission_operational_context
```

peut être préférable lorsque le workflow nécessite systématiquement
plusieurs informations cohérentes sur une mission.

Il faut cependant éviter les tools gigantesques qui retournent des
volumes de données inutiles.

------------------------------------------------------------------------

# 21. Contrats d'erreur

Les tools doivent retourner des erreurs structurées.

``` python
class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None
```

Exemples :

``` text
MISSION_NOT_FOUND
TMS_TIMEOUT
TMS_RATE_LIMIT
INVALID_ARGUMENT
TMS_UNAVAILABLE
PARTIAL_DATA
```

L'agent doit pouvoir distinguer :

``` text
"la mission n’existe pas"
```

de :

``` text
"je n’ai pas réussi à récupérer la mission"
```

Cette distinction est importante pour éviter qu'un LLM interprète une
erreur technique comme une information métier.

------------------------------------------------------------------------

# 22. PostgreSQL : ownership par domaine

Une même instance PostgreSQL peut être utilisée, tout en séparant les
responsabilités.

``` text
PostgreSQL
│
├── monitor
│   ├── processed_events
│   ├── monitoring_state
│   └── ...
│
├── agent
│   ├── alerts
│   ├── notifications
│   ├── driver_calls
│   └── ...
│
└── assistant
    ├── checkpoints
    └── ...
```

Le partage de la même base ne signifie pas partage libre des données.

Principe :

> Un composant ne doit pas accéder directement aux tables d'un autre
> domaine simplement parce qu'il peut techniquement le faire.

------------------------------------------------------------------------

# 23. Ownership des données

  -----------------------------------------------------------------------
  Donnée            Owner             Lecture           Écriture
  ----------------- ----------------- ----------------- -----------------
  Mission           TMS               MCP               TMS

  Position          TMS               MCP               TMS

  Events            TMS               MCP               TMS

  Rules             Monitoring        Monitoring        Configuration
                                                        métier

  Rule evaluation   Monitoring        Monitoring /      Monitoring
                                      audit             

  Alert             Alert domain      API / Agent /     Alert Service
                                      Audit             

  Notification      Notification      API / Audit       Notification
                    domain                              Service

  Investigation     Agent domain      API / Audit       Investigator

  Checkpoint        Assistant         Assistant Runtime Assistant Runtime

  Driver call       Agent /           API / Audit       Agent workflow
                    Operations                          
  -----------------------------------------------------------------------

Cette matrice doit être considérée comme une règle d'architecture avant
l'implémentation.

------------------------------------------------------------------------

# 24. MCP et PostgreSQL

Le principe important est :

> « MCP est l'unique accès au TMS » ne signifie pas « MCP doit être
> l'unique accès à PostgreSQL ».

Le MCP ne doit pas devenir un backend fourre-tout contenant :

``` text
TMS
+
Business logic
+
Alert persistence
+
Notification persistence
+
Agent runtime
+
Checkpoints
```

La persistence doit rester proche du domaine qui la possède.

``` text
Monitoring
    │
    ▼
AlertService
    │
    ▼
Repository
    │
    ▼
PostgreSQL
```

et non :

``` text
Monitoring
    │
    ▼
MCP
    │
    ▼
PostgreSQL
```

sauf lorsqu'une véritable raison d'architecture impose ce passage.

------------------------------------------------------------------------

# 25. Agent Server et PostgreSQL

L'Agent Service peut avoir besoin de PostgreSQL pour :

-   LangGraph checkpoints ;
-   état de conversation ;
-   investigations ;
-   éventuellement certaines données propres à son domaine.

Mais il ne doit pas devenir un accès SQL global.

``` text
Agent Service
│
├── Investigator
├── Assistant
├── MCP Client
└── Checkpointer
        │
        ▼
   assistant schema
```

Le Monitoring Service reste propriétaire de son propre état.

------------------------------------------------------------------------

# 26. API HTTP et MCP

Ces deux interfaces ne doivent pas être confondues.

## API HTTP

Destinée à :

-   frontend ;
-   application mobile ;
-   dashboard ;
-   intégrations applicatives ;
-   utilisateurs.

``` text
Frontend
   │
   ▼
Application API
   │
   ▼
Application Services
```

## MCP

Destiné principalement aux clients agentiques.

``` text
Agent Runtime
   │
   ▼
MCP Client
   │
   ▼
MCP Server
   │
   ▼
TMS
```

L'API HTTP n'a donc pas besoin de devenir un simple proxy du MCP.

------------------------------------------------------------------------

# 27. Découpage global du code

Le code de l'orchestrateur peut suivre une organisation explicite par
domaine.

``` text
src/
│
├── monitoring/
│   ├── scheduler.py
│   ├── snapshot_service.py
│   │
│   ├── rules/
│   │   ├── engine.py
│   │   ├── models.py
│   │   └── loader.py
│   │
│   ├── alerts/
│   │   ├── service.py
│   │   ├── models.py
│   │   └── repository.py
│   │
│   └── notifications/
│       ├── service.py
│       ├── models.py
│       └── repository.py
│
├── agents/
│   ├── investigator/
│   │   ├── graph.py
│   │   ├── prompts.py
│   │   ├── models.py
│   │   └── service.py
│   │
│   └── assistant/
│       ├── graph.py
│       ├── prompts.py
│       ├── models.py
│       └── service.py
│
├── integrations/
│   └── mcp/
│       ├── client.py
│       └── tools.py
│
├── persistence/
│   ├── monitor/
│   ├── agent/
│   └── assistant/
│
└── api/
    ├── alerts.py
    ├── missions.py
    └── assistant.py
```

MCP Server :

``` text
mcp_server/
│
├── connectors/
│   ├── base.py
│   └── tms.py
│
├── tools/
│   ├── missions.py
│   ├── tracking.py
│   ├── participants.py
│   └── operations.py
│
└── schemas/
    ├── mission.py
    ├── tracking.py
    └── common.py
```

------------------------------------------------------------------------

# 28. Principe de simplicité du code

L'architecture doit favoriser :

-   des services explicites ;
-   des repositories explicites ;
-   des contrats explicites ;
-   des workflows lisibles ;
-   des dépendances faciles à suivre ;
-   des tests indépendants ;
-   des responsabilités locales.

Il faut éviter les abstractions génériques introduites uniquement pour «
rendre le système extensible ».

L'extensibilité doit venir principalement des frontières métier bien
définies :

``` text
TMS Connector
Rules
Alert Service
Notification Service
Investigator
Assistant
```

et non d'une multiplication de classes abstraites.

------------------------------------------------------------------------

# 29. Workflow de monitoring complet

``` text
                    Scheduler
                        │
                        ▼
              Get active missions
                        │
                        ▼
             Get mission snapshots
                        │
                        ▼
                  Normalize
                        │
                        ▼
               Derived fields
                        │
                        ▼
                 Rules Engine
                        │
                 ┌──────┴──────┐
                 │             │
               No match      Match
                 │             │
                 ▼             ▼
                END       RuleEvaluation
                               │
                               ▼
                          Alert Service
                               │
                      ┌────────┴────────┐
                      │                 │
                  Duplicate          New alert
                      │                 │
                      ▼                 ▼
                     END             Alert
                                       │
                                       ▼
                                Notification
                                       │
                                       ▼
                                 Investigation
                                       │
                                       ▼
                                AI Investigator
```

L'investigation peut être exécutée de manière asynchrone afin de ne pas
ralentir la boucle de monitoring.

------------------------------------------------------------------------

# 30. Workflow d'investigation IA

``` text
Alert created
      │
      ▼
Investigation Request
      │
      ▼
Agent Runtime
      │
      ▼
Load context
      │
      ▼
LLM reasoning
      │
      ├───────────────┐
      ▼               ▼
   MCP tools       History
      │               │
      └───────┬───────┘
              ▼
        Evidence analysis
              │
              ▼
      InvestigationResult
              │
              ▼
          Persistence
              │
              ▼
        Operator / UI
```

Le workflow peut également intégrer une interaction avec le conducteur
lorsqu'une règle métier et les guardrails l'autorisent.

------------------------------------------------------------------------

# 31. Workflow Assistant

``` text
Operator
   │
   ▼
Question
   │
   ▼
Assistant Agent
   │
   ├── Understand intent
   │
   ├── Select tools
   │
   ├── Call MCP
   │
   ├── Retrieve history
   │
   ├── Reason
   │
   └── Synthesize
   │
   ▼
Structured / Natural Answer
```

L'assistant ne doit pas avoir la capacité implicite de modifier le TMS.

Toute action sensible doit passer par un tool explicitement exposé et
protégé.

------------------------------------------------------------------------

# 32. Workflow de développement réel

Le développement industriel doit suivre l'ordre métier suivant.

## Phase 1 --- Validation métier

Valider :

-   cycle de vie d'une mission ;
-   états ;
-   événements ;
-   anomalies ;
-   seuils ;
-   sévérités ;
-   règles d'escalade ;
-   destinataires ;
-   comportement attendu.

## Phase 2 --- Analyse du TMS

Documenter :

-   API ;
-   authentification ;
-   endpoints ;
-   pagination ;
-   filtres ;
-   événements ;
-   positions ;
-   données de mission ;
-   utilisateurs/destinataires ;
-   limitations ;
-   erreurs ;
-   performances.

## Phase 3 --- Contrat pivot

Définir :

-   `MissionSnapshot` ;
-   `PositionSnapshot` ;
-   `MissionEvent` ;
-   `RouteSnapshot` ;
-   `Recipient` ;
-   autres modèles nécessaires.

## Phase 4 --- TMS Connector + MCP

Construire :

``` text
TMS
 ↓
Connector
 ↓
Pivot models
 ↓
MCP tools
```

## Phase 5 --- Monitoring Core

Construire :

``` text
Scheduler
 ↓
Snapshot
 ↓
Rules
 ↓
Alert
 ↓
Notification
```

## Phase 6 --- Persistence et audit

Mettre en place :

-   états de monitoring ;
-   alertes ;
-   notifications ;
-   événements traités ;
-   audit ;
-   règles/version ;
-   investigations.

## Phase 7 --- Investigator

Ajouter :

``` text
Alert
 ↓
Investigation
 ↓
MCP tools
 ↓
LLM
 ↓
InvestigationResult
```

## Phase 8 --- Assistant

Ajouter :

``` text
Operator
 ↓
Assistant
 ↓
MCP
 ↓
LLM
```

## Phase 9 --- API et UI

Exposer :

-   missions ;
-   alertes ;
-   investigations ;
-   notifications ;
-   historique ;
-   assistant ;
-   rapports.

## Phase 10 --- Pilotage

Passer par :

``` text
Shadow mode
     ↓
Observation
     ↓
Analyse des faux positifs
     ↓
Ajustement des règles
     ↓
Pilot
     ↓
Production
```

------------------------------------------------------------------------

# 33. Principes de résilience

Le Monitoring Service doit être indépendant des composants LLM.

### TMS indisponible

``` text
TMS unavailable
      ↓
Cycle failed
      ↓
Backoff / retry
      ↓
Next cycle
```

Une panne sur une mission ne doit pas arrêter tout le cycle.

### LLM indisponible

``` text
LLM unavailable
      ↓
Investigation delayed/failed
      ↓
Alert remains valid
```

L'alerte ne doit pas disparaître simplement parce que l'analyse IA n'a
pas pu être produite.

### MCP indisponible

Le monitoring doit considérer le cycle comme incomplet/échoué selon la
nature de l'erreur, sans produire de fausses résolutions.

------------------------------------------------------------------------

# 34. Auditabilité

Pour chaque alerte importante, il doit être possible de retrouver :

``` text
Mission
   ↓
Snapshot
   ↓
Rule ID
   ↓
Rule version
   ↓
Observed data
   ↓
Rule evaluation
   ↓
Alert
   ↓
Notification
   ↓
Investigation
   ↓
Recommendation
```

L'objectif est de pouvoir répondre à :

> Pourquoi cette alerte a-t-elle été créée ?

et :

> Sur quelles données et quelle version de règle ?

Puis, séparément :

> Comment l'IA est-elle arrivée à cette analyse ?

------------------------------------------------------------------------

# 35. Règle d'or de l'architecture

L'architecture doit respecter les principes suivants :

``` text
TMS
= Source of Truth externe

MCP
= Frontière d’intégration TMS

Monitoring
= Décision déterministe

Rules Engine
= Traduction des règles métier

Alert Service
= Cycle de vie des alertes

Notification Service
= Communication

Agent Runtime
= Orchestration IA

LLM
= Raisonnement probabiliste

PostgreSQL
= État + audit de l’orchestrateur

API
= Interface applicative
```

Et surtout :

> Aucun composant ne doit devenir propriétaire d'une responsabilité
> simplement parce qu'il possède déjà une connexion technique à une
> ressource.

------------------------------------------------------------------------

# 36. Architecture cible synthétique

``` text
                              ┌─────────────┐
                              │     TMS     │
                              │ Source      │
                              │ of Truth    │
                              └──────┬──────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │ MCP Server  │
                              │             │
                              │ TMS only    │
                              └──────┬──────┘
                                     │
                    ┌────────────────┴────────────────┐
                    │                                 │
                    ▼                                 ▼
             ┌──────────────┐                  ┌──────────────┐
             │  Monitoring  │                  │ Agent Server │
             │   Service    │                  │              │
             │              │                  │ Investigator │
             │ Scheduler    │                  │ Assistant    │
             │ Rules        │                  │ LangGraph    │
             │ Alerts       │                  │ MCP Client   │
             │ Notifications│                  └──────┬───────┘
             └──────┬───────┘                         │
                    │                                 │
                    └────────────────┬────────────────┘
                                     │
                                     ▼
                              ┌─────────────┐
                              │ PostgreSQL  │
                              │             │
                              │ monitor     │
                              │ agent       │
                              │ assistant   │
                              └──────┬──────┘
                                     │
                                     ▼
                                ┌─────────┐
                                │   LLM   │
                                │  vLLM   │
                                │  Qwen   │
                                └─────────┘
```

Cette architecture permet de conserver une séparation nette entre :

1.  **la vérité métier externe** --- TMS ;
2.  **l'intégration** --- MCP ;
3.  **la décision déterministe** --- Monitoring + Rules ;
4.  **la persistence et l'audit** --- PostgreSQL ;
5.  **le raisonnement** --- Agent + LLM ;
6.  **l'expérience utilisateur** --- API + UI.

Elle constitue ainsi une base plus adaptée à une implémentation
production que de reproduire directement l'organisation du POC.




```
                        ┌──────────────────┐
                        │   Web / Client   │
                        └────────┬─────────┘
                                 │ HTTPS
                                 ▼
                        ┌──────────────────┐
                        │   API / Gateway  │
                        └────────┬─────────┘
                                 │
                         ┌───────┴────────┐
                         │                │
                         ▼                ▼
                ┌────────────────┐ ┌────────────────┐
                │ Agent Server   │ │ Monitoring     │
                │                │ │ Service        │
                │ Assistant      │ │                │
                │ Investigator   │ │ Scheduler      │
                │ LangGraph      │ │ Rules Engine   │
                └───────┬────────┘ └───────┬────────┘
                        │                  │
                        │ MCP              │ MCP
                        └────────┬─────────┘
                                 ▼
                       ┌──────────────────┐
                       │    MCP Server    │
                       │                  │
                       │ TMS Connector    │
                       │ Semantic Tools   │
                       │ Guardrails       │
                       └────────┬─────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │       TMS        │
                       └──────────────────┘


        ┌──────────────────┐       ┌──────────────────┐
        │   PostgreSQL     │       │      vLLM        │
        │                  │       │                  │
        │ monitor.*        │       │ Qwen             │
        │ agent.*          │       │ GPU              │
        │ assistant.*      │       │                  │
        └──────────────────┘       └──────────────────┘
```