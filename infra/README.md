# Infrastructure locale de développement (`infra/`)

Cette stack fait tourner, sur le poste de développement, les mêmes briques
d'infrastructure que la cible Azure. Elle sert à développer et tester
`monitoring_worker`, `tms_mcp_server`, `agent_service` et `ops_api` contre une
infra réelle, dès la phase de développement.

---

## 1. Principe : ce qui tourne dans Docker, et ce qui n'y tourne pas

| Service | Exécution locale | Équivalent de production |
| :--- | :--- | :--- |
| **PostgreSQL** | **natif sur l'hôte**, port `5432` | PostgreSQL Flexible Server |
| **Redis** | **natif sur l'hôte**, port `6379` | Azure Managed Redis |
| ClickHouse | conteneur | VM dédiée dans le VNet |
| MinIO | conteneur | Azure Blob Storage |
| Langfuse web + worker | conteneurs | Azure Container Apps |
| LiteLLM Proxy | conteneur | Azure Container Apps |
| vLLM | conteneur (profil `gpu`) | VM GPU (option 1) ou API managée (option 2) |

**PostgreSQL et Redis ne sont volontairement pas conteneurisés.** En production
ce sont des services managés, externes aux conteneurs applicatifs : les garder
natifs en local reproduit cette frontière réseau au lieu de la masquer derrière
un réseau Docker interne. Les conteneurs les joignent par
`host.docker.internal`, résolu grâce à `extra_hosts: host-gateway`.

Conséquence sur les URLs, selon l'appelant :

| Appelant | PostgreSQL | Redis |
| :--- | :--- | :--- |
| Code Python lancé sur l'hôte | `localhost:5432` | `localhost:6379` |
| Conteneur de la stack | `host.docker.internal:5432` | `host.docker.internal:6379` |

---

## 2. Bases de données et cloisonnement

Trois bases PostgreSQL distinctes, parce que Langfuse et LiteLLM gèrent
eux-mêmes leurs migrations et ne doivent jamais écrire dans la base métier :

| Base | Propriétaire | Contenu |
| :--- | :--- | :--- |
| `tms_agent_db` (schéma `orchestrator`) | nos services | alertes, outbox, enquêtes, checkpoints LangGraph |
| `langfuse_db` | Langfuse | ~450 migrations Prisma, utilisateurs, projets |
| `litellm_db` | LiteLLM | clés virtuelles, journal des dépenses |

Le Redis de l'hôte est partagé avec d'autres projets locaux : chaque
consommateur a donc sa **base logique** (voir `.env`).

| Base logique | Usage |
| :--- | :--- |
| `0` | autres projets locaux — ne pas utiliser |
| `1` | LiteLLM (cache, quotas) |
| `2` | Langfuse (file d'ingestion BullMQ) |
| `3` | tms-agent (cooldowns, verrous applicatifs) |

---

## 3. Inventaire des services

| Service | Conteneur | Port hôte | Accès | Rôle |
| :--- | :--- | :--- | :--- | :--- |
| LiteLLM Proxy | `tms-litellm-proxy` | `4000` | `http://localhost:4000` | Passerelle LLM compatible OpenAI, repli, traces |
| vLLM | `tms-vllm-qwen` | `8001` | `http://localhost:8001` | Inférence locale GPU (profil `gpu`) |
| Langfuse Web | `tms-langfuse-web` | `3000` | `http://localhost:3000` | Interface et ingestion OTLP des traces LLM |
| Langfuse Worker | `tms-langfuse-worker` | `3030` | interne | Ingestion vers ClickHouse |
| ClickHouse | `tms-clickhouse` | `8123`, `9000` | `127.0.0.1` | Stockage analytique des traces |
| MinIO | `tms-minio` | `9090`, `9091` | `http://localhost:9091` | Stockage objet S3 (console web sur `9091`) |

Identifiants Langfuse en local : l'organisation, le projet `tms-agent`, les clés
API et le compte utilisateur sont provisionnés automatiquement au premier
démarrage à partir des variables `LANGFUSE_INIT_*` du `.env`.

---

## 4. Exploitation

Toutes les commandes passent par `scripts/infra.sh`, qui charge le `.env` de la
racine, vérifie que PostgreSQL et Redis répondent, crée les bases manquantes,
puis démarre les conteneurs. Le profil `gpu` est activé automatiquement si un
GPU NVIDIA est détecté.

```bash
scripts/infra.sh up          # démarre tout
scripts/infra.sh up --no-gpu # sans vLLM (LiteLLM basculera sur le repli cloud)
scripts/infra.sh status
scripts/infra.sh logs langfuse-web
scripts/infra.sh down
scripts/infra.sh reset       # supprime aussi les volumes ClickHouse / MinIO
scripts/infra.sh check       # vérifie la santé fonctionnelle de tout
```

`scripts/check_infra.sh` ne se contente pas de tester des ports : il écrit dans
PostgreSQL et Redis, pose un verrou consultatif, interroge ClickHouse
authentifié, vérifie l'accès hôte depuis un conteneur, lance une vraie
complétion LLM à travers LiteLLM et confirme que la trace est bien arrivée dans
Langfuse.

Équivalent sans script :

```bash
docker compose --env-file ../.env --profile gpu up -d --remove-orphans
```

---

## 5. Inférence LLM

Le code applicatif n'appelle qu'un nom logique de modèle, jamais un fournisseur.
Le routage vit dans `litellm.yaml` :

| Nom logique | Cible | Usage |
| :--- | :--- | :--- |
| `mission-investigator` | vLLM local (`local-qwen`) | primaire |
| `mission-investigator-backup` | Groq `qwen/qwen3.8-27b` | repli automatique |
| `mission-investigator-cloud` | Groq `qwen/qwen3.8-27b`, en direct | tests sans GPU |

Le repli utilise **Qwen3.8-27B**, le modèle retenu pour l'option 2 de
l'architecture cible : le chemin de secours de dev exerce donc déjà le modèle
de production.

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"mission-investigator","messages":[{"role":"user","content":"ping"}]}'
```

**Contrainte matérielle du poste de dev :** RTX 4070 Laptop, 8 Go de VRAM.
`Qwen2.5-7B-Instruct-AWQ` ne tient qu'avec `--max-model-len=4096` ; au-delà, le
cache KV ne rentre plus et vLLM refuse de démarrer. La cible Azure (A10 24 Go ou
A100 80 Go) permettra le contexte complet — seules les variables `VLLM_*` du
`.env` changent, pas le code.

---

## 6. Observabilité

Langfuse 4 **n'ingère les traces que par OTLP**, sur
`/api/public/otel/v1/traces` ; l'ancien endpoint `/api/public/ingestion` les
refuse désormais (il n'accepte plus que les scores). C'est pourquoi LiteLLM est
configuré avec le callback `langfuse_otel`, déclaré sous
`litellm_settings.callbacks` et non `success_callback` — les intégrations de type
OpenTelemetry n'instancient leur exporteur que de cette façon.

Les traces sont stockées par ClickHouse dans `events_core` / `events_full` (le
modèle d'événements de la v4). Les tables `traces` et `observations`, héritées de
la v3, restent vides : ne pas s'en servir pour vérifier l'ingestion.

```bash
# Événements ingérés, avec les tokens consommés
curl -s "http://localhost:8123/?user=clickhouse&password=clickhouse" --data-binary \
  "SELECT name, provided_model_name, usage_details['input'], usage_details['output'] \
   FROM events_core ORDER BY start_time DESC LIMIT 10 FORMAT TSV"
```

---

## 7. Dépannage

| Symptôme | Cause et correctif |
| :--- | :--- |
| `conteneur → PostgreSQL hôte` en échec | `pg_hba.conf` / `listen_addresses` n'autorisent pas le réseau Docker. Autoriser la plage Docker, puis `sudo systemctl reload postgresql`. |
| Langfuse redémarre en boucle sur ClickHouse | Mot de passe divergent entre le `.env` et le volume ClickHouse déjà initialisé → `scripts/infra.sh reset`. |
| vLLM s'arrête au démarrage | VRAM insuffisante : baisser `VLLM_MAX_MODEL_LEN` ou `VLLM_GPU_MEMORY_UTILIZATION` dans le `.env`. |
| LiteLLM refuse de démarrer | `LITELLM_MASTER_KEY` absent du `.env`. |
| Aucune trace dans Langfuse | Callback LiteLLM mal déclaré (cf. §6) ou clés `LANGFUSE_INIT_PROJECT_*` divergentes. |
| `maxmemory-policy` ≠ `noeviction` | La file d'ingestion Langfuse l'exige : `redis-cli config set maxmemory-policy noeviction`. |

Le fichier `langfuse/docker-compose.upstream.reference.yml` est la stack
officielle de Langfuse, conservée uniquement comme référence. Elle n'est pas
incluse : elle conteneurise PostgreSQL et Redis, ce que cette installation
écarte volontairement.
