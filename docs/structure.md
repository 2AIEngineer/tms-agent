```
tms-agent/               
├── pyproject.toml
├── README.md
│
│   # -------------------------------------------------------------------------
│   # CONTRATS D'ÉCHANGE INTER-SERVICES (Pivots minimaux stricts uniquement)
│   # -------------------------------------------------------------------------
├── shared/
│   └── mapped_dto/            # Les seuls DTOs partagés entre conteneurs
│       ├── __init__.py
│       ├── snapshot.py             # MissionSnapshot, VehicleSnapshot, DriverSnapshot
│       ├── events.py               # MissionEvent, EventType
│       ├── alert.py                # Alert, Severity, AlertStatus
│       └── errors.py               # ToolError standardisé MCP
│
│   # -------------------------------------------------------------------------
│   # SERVICES DÉPLOYABLES (Chacun est hermétique et possède son domaine)
│   # -------------------------------------------------------------------------
├── services/
│   │
│   ├── monitoring/          # 1. MONITORING (Tout ce qui concerne la détection)
│   │   ├── main.py                 # Entrée du worker (boucle + lock DB)
│   │   ├── config.py               # Configuration spécifique au monitoring
│   │   │
│   │   ├── rules/                  # << DÉPLACÉ ICI : Domaine 100% propre au monitoring
│   │   │   ├── definitions/        # Fichiers YAML déclaratifs
│   │   │   │   ├── deviations.yaml
│   │   │   │   ├── delays.yaml
│   │   │   │   └── immobilizations.yaml
│   │   │   ├── schema.py           # Modèles Pydantic pour valider une règle YAML
│   │   │   ├── engine.py           # Moteur d'évaluation déterministe
│   │   │   ├── loader.py           # Chargement et rechargement à chaud
│   │   │   └── operators/          # Opérateurs métiers
│   │   │       ├── temporal.py
│   │   │       ├── geospatial.py
│   │   │       └── comparison.py
│   │   │
│   │   ├── snapshots/              # Récupération & normalisation périodique
│   │   │   ├── poller.py
│   │   │   └── state.py
│   │   │
│   │   ├── alerts/                 # Gestion du cycle de vie des alertes
│   │   │   ├── service.py          # Déduplication, cooldown, auto-resolve
│   │   │   └── repository.py       # Persistence PostgreSQL (tables alerts)
│   │   │
│   │   └── notifications/          # Envoi direct & Outbox
│   │       ├── service.py
│   │       ├── outbox.py           # Transaction outbox
│   │       └── channels/           # SMS, In-App, Email
│   │
│   ├── mcp_server/             # 2. MCP SERVER (Frontière TMS avec FastMCP)
│   │   ├── server.py               # Instance FastMCP
│   │   ├── config.py
│   │   ├── connectors/             # Client TMS externe (GET, timeouts, retries)
│   │   │   ├── base.py
│   │   │   └── rest_api_tms.py
│   │   ├── mappers/                # Traduction TMS JSON brut -> pivot_contracts
│   │   │   └── snapshot_mapper.py
|   |   |   └── events_mapper.py
│   │   ├── tools/                  # Outils exposés via @mcp.tool
│   │   │   ├── missions.py
│   │   │   ├── tracking.py
│   │   │   ├── participants.py
│   │   │   └── operations.py       # call_driver (idempotent)
│   │   └── guardrails.py           # Quotas & rate-limiting vers le TMS
│   │
│   ├── agents/              # 3. AGENT SERVICE (Intelligence & Enquêtes)
│   │   ├── main.py                 # Consommateur file d'enquêtes DB
│   │   ├── config.py
│   │   ├── integrations/
│   │   │   ├── mcp_client.py       # Appelle le mcp_server
│   │   │   └── litellm_client.py   # Client LiteLLM + Langfuse
│   │   ├── investigator/           # Workflow d'enquête autonome
│   │   │   ├── graph.py            # LangGraph StateGraph
│   │   │   ├── state.py            # Schéma d'état interne de l'enquête
│   │   │   ├── prompts.py          # Prompts système
│   │   │   └── schema.py           # InvestigationResult (évaluation finale)
│   │   └── assistant/              # Workflow conversationnel opérateur
│   │       ├── graph.py
│   │       ├── prompts.py
│   │       └── checkpointer.py     # Checkpointer PostgreSQL LangGraph
│   │
│   └── ops_api/                    # 4. OPS API (Passerelle FastAPI publique)
│       ├── main.py
│       ├── config.py
│       ├── auth/                   # Entra ID JWT + RBAC
│       │   ├── jwt.py
│       │   └── roles.py
│       ├── routers/
│       │   ├── missions.py
│       │   ├── alerts.py
│       │   ├── assistant.py
│       │   └── sse.py              # Flux temps réel des alertes vers un frontend
│       └── schemas/                # Schémas DTO propres à l'API HTTP
│
│   # -------------------------------------------------------------------------
│   # PERSISTANCE & MIGRATIONS SQL
│   # -------------------------------------------------------------------------
├── database/
│   ├── alembic/
│   │   └── versions/
│   └── alembic.ini
│
└── tests/
    ├── unit/
    │   ├── monitoring/             # Tests unitaires du moteur de règles & cooldowns
    │   ├── mcp/
    │   └── agent/
    ├── integration/

```