```
tms-agent/               
├── pyproject.toml
├── README.md
│
│   # -------------------------------------------------------------------------
│   # CONTRATS D'ÉCHANGE INTER-SERVICES (Pivots minimaux stricts uniquement)
│   # -------------------------------------------------------------------------
├── shared/
│   └── mapped_dto/            # Les seuls DTOs partagés entre conteneurs   [FAIT]
│       ├── __init__.py
│       ├── base.py                 # Pivot (strict + immuable), Position, Place
│       ├── snapshot.py             # MissionSnapshot, Vehicle/Driver/Deviation/Eta...
│       ├── events.py               # MissionEvent, EventType, EventSource
│       ├── alert.py                # Alert, Severity (ordonnée), Notification*
│   └── utils/                                                              [FAIT]
│       ├── db.py                   # Moteur, transactions, verrous consultatifs
│       ├── geo.py                  # Haversine, partagé MCP / moteur de règles
│       └── serialization.py        # JSON <-> Python (dates, énumérations)
│   └── channels/                   # Canaux d'envoi (couture fournisseur)   [FAIT]
│       ├── base.py                 # Protocole Channel, Message, SendResult
│       └── console.py              # Canal de développement (journalise)
│       ├── evaluation.py           # RuleEvaluation, RuleAction
│       ├── investigation.py        # InvestigationRequest/Result (F21-F22)
│       └── errors.py               # ToolError standardisé MCP
│
│   # -------------------------------------------------------------------------
│   # SERVICES DÉPLOYABLES (Chacun est hermétique et possède son domaine)
│   # -------------------------------------------------------------------------
├── services/
│   │
│   ├── monitoring/          # 1. MONITORING (détection déterministe)
│   │   ├── main.py                 # Worker : 1 boucle par TMS + expédition,    [FAIT]
│   │   │                           #   verrou par TMS = répartition entre
│   │   │                           #   instances, backoff par TMS, SIGTERM
│   │   ├── scheduler.py            # Le cycle de surveillance (F1-F8)           [FAIT]
│   │   ├── state.py                # Battement de cœur : panne silencieuse      [FAIT]
│   │   ├── config.py               # Configuration spécifique au monitoring
│   │   │
│   │   ├── rules/                  # Domaine 100% propre au monitoring       [FAIT]
│   │   │   ├── definitions/        # 6 règles YAML déclaratives
│   │   │   ├── schema.py           # Modèles Pydantic pour valider une règle YAML
│   │   │   ├── engine.py           # Moteur d'évaluation déterministe
│   │   │   ├── loader.py           # Chargement + rechargement à chaud (RuleRepository)
│   │   │   ├── context.py          # Champs dérivés : snapshot -> contexte d'évaluation
│   │   │   ├── paths.py            # Résolution d'un chemin pointé, MISSING
│   │   │   ├── template.py         # Rendu `{{ chemin }}`, sans code exécutable
│   │   │   └── operators/          # Opérateurs métiers (15, registre)
│   │   │       ├── base.py         # Operator, registre, MISSING, InvalidRuleValue
│   │   │       ├── temporal.py
│   │   │       ├── geospatial.py
│   │   │       └── comparison.py
│   │   │
│   │   ├── snapshots/              # Récupération & normalisation périodique
│   │   │   ├──  
│   │   │   └──  
│   │   │
│   │   ├── config.py               # Réglages du service (pydantic-settings)   [FAIT]
│   │   ├── alerts/                 # Cycle de vie des alertes                   [FAIT]
│   │   │   ├── service.py          # Déduplication, cooldown, clôture auto
│   │   │   ├── repository.py       # Persistance PostgreSQL explicite
│   │   │   └── models.py           # Bilan de cycle, raisons de suppression
│   │   │
│   │   ├── notifications/          # Envoi des notifications (F15-F18)      [FAIT]
│   │   │   ├── service.py          # Consommateur de file, reprises, statuts
│   │   │   ├── outbox.py           # File : réclamation par bail, SKIP LOCKED
│   │   │   └── models.py           # OutboxEntry, SentNotification, bilan
│   │   │
│   │   └── integrations/                                                   [FAIT]
│   │       └── mcp_client.py       # Seule porte du monitoring vers le TMS,
│   │                               #   rend des objets pivot revalidés
│   │
│   ├── mcp_server/             # 2. MCP SERVER (Frontière TMS)              [FAIT]
│   │   ├── server.py               # Instance FastMCP, transport HTTP sans état
│   │   ├── config.py
│   │   ├── context.py              # Dépendances partagées par les outils
│   │   ├── snapshots.py            # Assemblage parallèle des instantanés (F2)
│   │   ├── recipients.py           # Résolution rôle -> destinataire TMS (F16)
│   │   ├── capability.py           # Rapport de capacité d'un TMS            [FAIT]
│   │   ├── connections.py          # Registre des TMS raccordés (multi-TMS)  [FAIT]
│   │   ├── cli.py                  # list / validate / report                [FAIT]
│   │   ├── connectors/             # Client TMS externe (GET seul, timeouts, retries)
│   │   │   ├── base.py             # Protocole TmsConnector, TmsFailure
│   │   │   ├── dialect.py          # Schéma d'un dialecte + validation       [FAIT]
│   │   │   ├── http_tms.py         # Connecteur générique piloté par dialecte [FAIT]
│   │   │   ├── profile.py          # TmsProfile : trappe de code par TMS     [FAIT]
│   │   │   ├── paths.py            # Extraction par chemin pointé
│   │   │   ├── rest_api_tms.py     # x_tms = HttpTmsConnector + son dialecte
│   │   │   └── dialects/           # Un fichier YAML par TMS raccordé
│   │   │       ├── x_tms.yaml
│   │   │       └── fleetlink.yaml  # TMS fictif de validation
│   │   ├── mappers/                # Traduction pilotée par le dialecte
│   │   │   └── pivot.py            # PivotMapper + champs dérivés (haversine)
│   │   ├── tools/                  # Outils exposés via @mcp.tool
│   │   │   ├── missions.py         # get_active_missions, get_mission_snapshot...
│   │   │   ├── tracking.py         # get_tms_time, positions, itinéraire
│   │   │   ├── monitoring.py       # get_monitoring_cycle : 1 appel par cycle
│   │   │   └── registry.py         # list_tms : découverte + capacités       [FAIT]
│   │   │   └── participants.py     # chauffeur, véhicule, resolve_recipients
│   │   │                           # (call_driver vit côté agent : le TMS est
│   │   │                           #  en lecture seule, un appel n'est pas une
│   │   │                           #  opération TMS — cf. README)
│   │
│   ├── agents/              # 3. AGENT SERVICE (Intelligence & Enquêtes)
│   │   ├── main.py                 # Consommateur d'enquêtes               [FAIT]
│   │   ├── config.py                                                       [FAIT]
│   │   ├── integrations/
│   │   │   ├── mcp_client.py       # Découverte dynamique des outils       [FAIT]
│   │   │   ├── litellm_client.py   # Inférence + contrat d'erreur          [FAIT]
│   │   │   ├── reducers.py         # Réduction par le sens des résultats   [FAIT]
│   │   │   ├── budget.py           # Compaction du contexte avant envoi    [FAIT]
│   │   │   └── (litellm_client)    # `complete_stream` : diffusion + fragments [FAIT]
│   │   ├── investigator/           # Workflow d'enquête autonome
│   │   │   ├── repository.py       # Réclamation atomique des enquêtes    [FAIT]
│   │   │   ├── facts.py            # Faits déterministes (F21)            [FAIT]
│   │   │   ├── loop.py             # Boucle bornée, conclusion forcée      [FAIT]
│   │   │   ├── submission.py       # Schéma contraint dérivé du contrat    [FAIT]
│   │   │   └── prompts.py          # Prompts système                       [FAIT]
│   │   ├── local_tools/            # Lectures de notre domaine (F21)
│   │   │   ├── history.py          # Récurrence : alertes, enquêtes, décisions [FAIT]
│   │   │   └── rules.py            # « la condition tient-elle encore ? »       [FAIT]
│   │   ├── operations/             # Les actions, par opposition aux lectures
│   │   │   ├── driver_call.py      # Garde-fous : idempotence, quotas       [FAIT]
│   │   │   └── tool.py             # call_driver, outil local du modèle     [FAIT]
│   │   └── assistant/              # Workflow conversationnel opérateur
│   │       ├── service.py          # Boucle bornée, lectures puis réponse     [FAIT]
│   │       ├── repository.py       # Historique en base (F25, sans LangGraph) [FAIT]
│   │       ├── actions.py          # Clôture, appel, décision (F26)           [FAIT]
│   │       ├── pending.py          # Validation humaine : proposer / valider  [FAIT]
│   │       ├── tools.py            # Lectures ouvertes / actions autorisées   [FAIT]
│   │       ├── prompts.py          # Prompts par mode                         [FAIT]
│   │       ├── schedule.py         # Rapport à heure fixe, un créneau une fois [FAIT]
│   │       └── cli.py              # Entrée en ligne de commande              [FAIT]
│   │
│   └── ops_api/                    # 4. OPS API (Passerelle FastAPI publique)
│       ├── main.py                 # Montage, dégradation sans MCP          [FAIT]
│       ├── config.py                                                        [FAIT]
│       ├── dependencies.py         # Deux portes : require_read / require_act [FAIT]
│       ├── repository.py           # Lectures seules (alertes, audit, état)  [FAIT]
│       ├── mcp.py                  # Façade MCP, périmètre appliqué          [FAIT]
│       ├── streaming.py            # Acheminement SSE + maintien en vie      [FAIT]
│       ├── auth/                   # Entra ID JWT + RBAC
│       │   ├── jwt.py              # RS256, audience, émetteur, expiration   [FAIT]
│       │   └── roles.py            # Rôles du TMS -> lire / agir             [FAIT]
│       ├── routers/
│       │   ├── missions.py                                                  [FAIT]
│       │   ├── alerts.py                                                    [FAIT]
│       │   ├── assistant.py                                                 [FAIT]
│       │   ├── health.py           # /health, /monitoring                    [FAIT]
│       │   └── sse.py              # Flux temps réel des alertes (curseur)    [FAIT]
│       └── schemas/                # Schémas DTO propres à l'API HTTP
│
│   # -------------------------------------------------------------------------
│   # PERSISTANCE & MIGRATIONS SQL
│   # -------------------------------------------------------------------------
├── database/                                                                [FAIT]
│   ├── schema.py                   # Tables SQLAlchemy Core, source unique
│   ├── alembic/
│   │   └── versions/               # 5 migrations : alertes, outbox, audit,
│   │                               #   notifications, battement de cœur,
│   │                               #   dimension tms_id, enquêtes
│   └── alembic.ini
│
├── scripts/
│   ├── infra.sh                    # Pilotage de la stack d'infra locale      [FAIT]
│   ├── check_infra.sh              # Vérification fonctionnelle de l'infra     [FAIT]
│   ├── test.sh                     # Suite de tests (isole le PYTHONPATH ROS)  [FAIT]
│   └── tms.sh                      # Pilote le TMS simulé : status, reset,     [FAIT]
│                                   #   serve, client, start, demo
│
└── tests/
    ├── factories.py                # Fabriques de MissionSnapshot              [FAIT]
    ├── conftest.py                 # Fixtures partagées                        [FAIT]
    ├── fake_tms.py                 # Double fidèle du TMS (transport httpx)   [FAIT]
    ├── fake_fleetlink.py           # Double d'un TMS aux noms opposés         [FAIT]
    ├── fake_llm.py                 # Modèle scripté (égarements, pannes)      [FAIT]
    ├── unit/
    │   ├── shared/                 # Contrats pivot                            [FAIT]
    │   ├── monitoring/             # Moteur de règles, opérateurs, chargeur    [FAIT]
    │   ├── mcp/                    # Connecteur, mappers, garde-fous           [FAIT]
    │   └── agents/                 # Faits déterministes, boucle d'enquête     [FAIT]
    ├── integration/                                                          [FAIT]
    │   ├── test_alert_service.py    # Alertes sur PostgreSQL réel
    │   ├── test_mcp_tools.py        # Outils via le protocole MCP réel
    │   ├── test_notifications.py    # Chaîne alerte -> file -> MCP -> canal
    │   ├── test_scheduler.py        # Cycle, battement de cœur, instance unique
    │   ├── test_multi_tms.py        # Collisions d'identifiants entre TMS
    │   ├── test_worker_distribution.py  # Verrous par TMS, répartition
    │   ├── test_investigation_queue.py  # Réclamation atomique des enquêtes
    │   └── test_live_tms.py         # Contre x_tms en écoute (ignoré sinon)

```