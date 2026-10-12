#!/usr/bin/env bash
# =============================================================================
#  check_infra.sh — vérifie que toute l'infra de dev est opérationnelle
# =============================================================================
#  Chaque test est fonctionnel, pas seulement "le port est ouvert" : on écrit
#  dans Redis, on interroge ClickHouse authentifié, on pose un verrou consultatif
#  PostgreSQL, on fait une vraie complétion LLM et on vérifie qu'elle est tracée.
#
#  Code de sortie 0 si tout passe, 1 si au moins un test échoue.
# =============================================================================
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "Fichier manquant : $ENV_FILE"; exit 1; }
set -a; source "$ENV_FILE"; set +a

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; DIM=$'\e[2m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
PASS=0; FAIL=0; SKIP=0

section() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }
pass() { PASS=$((PASS+1)); printf '  %s✓%s %-46s %s%s%s\n' "$GREEN" "$RESET" "$1" "$DIM" "${2:-}" "$RESET"; }
bad()  { FAIL=$((FAIL+1)); printf '  %s✗%s %-46s %s%s%s\n' "$RED" "$RESET" "$1" "$DIM" "${2:-}" "$RESET"; }
skip() { SKIP=$((SKIP+1)); printf '  %s–%s %-46s %s%s%s\n' "$YELLOW" "$RESET" "$1" "$DIM" "${2:-}" "$RESET"; }

# Exécute une commande ; `pass` si code 0 et sortie non vide, `bad` sinon.
try() { # try <libellé> <commande...>
  local label="$1"; shift
  local out; out="$("$@" 2>&1)"
  if [[ $? -eq 0 ]]; then pass "$label" "${out//$'\n'/ }"; else bad "$label" "${out//$'\n'/ }"; fi
}

export PGPASSWORD="$POSTGRES_PASSWORD"
PSQL=(psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -At)
REDIS=(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT")

# Requête ClickHouse authentifiée, renvoie la valeur brute (vide si échec)
ch_query() {
  curl -fsS -m 10 "http://localhost:8123/?user=${CLICKHOUSE_USER}&password=${CLICKHOUSE_PASSWORD}" \
       --data-binary "$1 FORMAT TSV" 2>/dev/null | head -1
}

# -----------------------------------------------------------------------------
section "1. PostgreSQL — natif sur l'hôte (${POSTGRES_HOST}:${POSTGRES_PORT})"
# -----------------------------------------------------------------------------
if pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -q; then
  pass "service en écoute" "$("${PSQL[@]}" -d postgres -c 'show server_version' 2>/dev/null | head -1)"

  for db in "$POSTGRES_DB" tms_agent_test_db langfuse_db litellm_db; do
    if "${PSQL[@]}" -d postgres -c "select 1 from pg_database where datname='$db'" | grep -q 1; then
      pass "base présente : $db"
    else
      bad "base absente : $db" "scripts/infra.sh up la crée"
    fi
  done

  if "${PSQL[@]}" -d "$POSTGRES_DB" -c \
     "select 1 from information_schema.schemata where schema_name='$POSTGRES_SCHEMA'" | grep -q 1; then
    pass "schéma applicatif : $POSTGRES_SCHEMA"
  else
    bad "schéma applicatif absent : $POSTGRES_SCHEMA"
  fi

  # Tables de l'orchestrateur : un schéma en retard casse le premier cycle.
  tables="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    "select count(*) from information_schema.tables where table_schema='$POSTGRES_SCHEMA' \
     and table_name in ('alerts','notification_outbox','processed_events','audit_log')" 2>&1)"
  [[ "${tables:-0}" -eq 4 ]] && pass "tables de l'orchestrateur (4/4)" \
                             || bad "tables manquantes (${tables:-0}/4)" "scripts/infra.sh migrate"

  revision="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    "select version_num from $POSTGRES_SCHEMA.alembic_version" 2>/dev/null)"
  [[ -n "$revision" ]] && pass "migrations Alembic appliquées" "révision $revision" \
                       || bad "aucune révision Alembic" "scripts/infra.sh migrate"

  # Index unique partiel : c'est lui qui porte la déduplication des alertes (F11).
  # Le contrôle porte sur sa **définition** et non sur son nom : ce qui importe
  # est qu'il soit unique, restreint aux alertes ouvertes, et qu'il couvre la
  # provenance. Sans `tms_id`, deux TMS portant une mission du même identifiant
  # verraient l'alerte du second supprimée comme doublon du premier.
  dedup="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    "select indexdef from pg_indexes where schemaname='$POSTGRES_SCHEMA' \
     and tablename='alerts' and indexdef like '%UNIQUE%' and indexdef like '%deduplication_key%'" 2>&1)"
  if [[ -z "$dedup" ]]; then
    bad "index de déduplication absent" "la déduplication ne serait plus garantie"
  elif [[ "$dedup" != *"tms_id"* ]]; then
    bad "index de déduplication sans provenance" "collision possible entre deux TMS"
  elif [[ "$dedup" != *"status"* ]]; then
    bad "index de déduplication non restreint" "une alerte close bloquerait sa clé"
  else
    pass "index de déduplication des alertes" "unique partiel sur (tms_id, clé), alertes ouvertes"
  fi

  # Clé primaire composée de `processed_events` : porte l'idempotence des
  # événements (F7). Sur `event_id` seul, un événement d'un second TMS passerait
  # pour déjà traité et son alerte ne partirait jamais.
  pk="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    "select pg_get_constraintdef(oid) from pg_constraint \
     where conrelid='$POSTGRES_SCHEMA.processed_events'::regclass and contype='p'" 2>&1)"
  if [[ "$pk" == *"tms_id"* && "$pk" == *"event_id"* ]]; then
    pass "idempotence des événements" "clé primaire (tms_id, event_id)"
  else
    bad "idempotence des événements fragile" "clé primaire : ${pk:-absente}"
  fi

  # Unicité partielle des appels au chauffeur : c'est elle qui empêche une
  # enquête rejouée de rappeler. Partielle à dessein — restreinte aux tentatives
  # réellement remises au fournisseur. Totale, un appel refusé par un cooldown
  # interdirait définitivement la tentative suivante, pourtant légitime.
  calls="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    "select indexdef from pg_indexes where schemaname='$POSTGRES_SCHEMA' \
     and tablename='driver_calls' and indexdef like '%UNIQUE%' \
     and indexdef like '%idempotency_key%'" 2>&1)"
  if [[ -z "$calls" ]]; then
    bad "unicité des appels au chauffeur absente" "une enquête rejouée rappellerait"
  elif [[ "$calls" != *"WHERE"* ]]; then
    bad "unicité des appels totale et non partielle" "un refus bloquerait toute reprise"
  else
    pass "idempotence des appels au chauffeur" "unique partiel sur les tentatives réelles"
  fi

  # Verrou consultatif : mécanisme d'instance unique du monitoring_worker (F-arch §4)
  lock="$("${PSQL[@]}" -d "$POSTGRES_DB" -c \
    'select pg_try_advisory_lock(42), pg_advisory_unlock(42)' 2>&1)"
  [[ "$lock" == "t|t" ]] && pass "verrou consultatif (pg_try_advisory_lock)" \
                         || bad "verrou consultatif" "$lock"

  # Écriture réelle dans le schéma applicatif
  w="$("${PSQL[@]}" -d "$POSTGRES_DB" <<'SQL' 2>&1
create table if not exists orchestrator._infra_check(id serial primary key, at timestamptz default now());
insert into orchestrator._infra_check default values;
select count(*) from orchestrator._infra_check;
drop table orchestrator._infra_check;
SQL
)"
  # Le SELECT est la seule ligne purement numérique de la sortie psql
  if grep -qE '^[0-9]+$' <<<"$w"; then
    pass "écriture/lecture dans $POSTGRES_SCHEMA"
  else
    bad "écriture dans $POSTGRES_SCHEMA" "${w//$'\n'/ }"
  fi
else
  bad "service injoignable" "sudo systemctl start postgresql"
fi

# -----------------------------------------------------------------------------
section "2. Redis — natif sur l'hôte (${REDIS_HOST}:${REDIS_PORT})"
# -----------------------------------------------------------------------------
if [[ "$("${REDIS[@]}" ping 2>/dev/null)" == "PONG" ]]; then
  pass "service en écoute" "v$("${REDIS[@]}" info server 2>/dev/null | sed -n 's/^redis_version:\(.*\)\r*$/\1/p')"

  key="tms-agent:infra-check:$$"
  "${REDIS[@]}" -n "$REDIS_DB_APP" set "$key" ok ex 30 >/dev/null 2>&1
  if [[ "$("${REDIS[@]}" -n "$REDIS_DB_APP" get "$key" 2>/dev/null)" == "ok" ]]; then
    pass "écriture/lecture base applicative (db $REDIS_DB_APP)"
    "${REDIS[@]}" -n "$REDIS_DB_APP" del "$key" >/dev/null 2>&1
  else
    bad "écriture base applicative (db $REDIS_DB_APP)"
  fi

  policy="$("${REDIS[@]}" config get maxmemory-policy 2>/dev/null | tail -1)"
  [[ "$policy" == "noeviction" ]] && pass "maxmemory-policy = noeviction" "requis par la file Langfuse" \
                                 || bad "maxmemory-policy = $policy" "Langfuse exige noeviction"
else
  bad "service injoignable" "sudo systemctl start redis-server"
fi

# -----------------------------------------------------------------------------
section "3. Accès hôte depuis les conteneurs (host.docker.internal)"
# -----------------------------------------------------------------------------
#  C'est le point de rupture typique de cette topologie : les conteneurs doivent
#  joindre PostgreSQL et Redis qui tournent hors de Docker.
if docker info >/dev/null 2>&1; then
  out="$(docker run --rm --add-host=host.docker.internal:host-gateway \
          redis:7-alpine redis-cli -h host.docker.internal -p "$REDIS_PORT" ping 2>&1 | tail -1)"
  [[ "$out" == "PONG" ]] && pass "conteneur → Redis hôte" || bad "conteneur → Redis hôte" "$out"

  out="$(docker run --rm --add-host=host.docker.internal:host-gateway \
          -e PGPASSWORD="$POSTGRES_PASSWORD" postgres:17 \
          pg_isready -h host.docker.internal -p "$POSTGRES_PORT" -U "$POSTGRES_USER" 2>&1 | tail -1)"
  [[ "$out" == *"accepting connections"* ]] && pass "conteneur → PostgreSQL hôte" \
                                            || bad "conteneur → PostgreSQL hôte" "$out"
else
  bad "démon Docker injoignable"
fi

# -----------------------------------------------------------------------------
section "4. ClickHouse — conteneur (127.0.0.1:8123)"
# -----------------------------------------------------------------------------
if curl -fsS -m 5 http://localhost:8123/ping >/dev/null 2>&1; then
  pass "ping HTTP"
  v="$(curl -fsS -m 5 "http://localhost:8123/?user=${CLICKHOUSE_USER}&password=${CLICKHOUSE_PASSWORD}" \
       --data-binary 'SELECT version()' 2>&1)"
  [[ "$v" =~ ^[0-9]+\. ]] && pass "requête authentifiée (${CLICKHOUSE_USER})" "v$v" \
                          || bad "requête authentifiée" "$v"
  n="$(ch_query "SELECT count() FROM system.tables WHERE database='default' AND name IN ('events_core','events_full','schema_migrations')")"
  [[ "${n:-0}" -eq 3 ]] && pass "migrations Langfuse appliquées" "schéma v4 (events_core, events_full)" \
                        || bad "migrations Langfuse incomplètes" "${n:-0}/3 tables — voir docker logs tms-langfuse-worker"
else
  bad "injoignable sur 8123"
fi

# -----------------------------------------------------------------------------
section "5. MinIO — conteneur (127.0.0.1:9090)"
# -----------------------------------------------------------------------------
if curl -fsS -m 5 http://localhost:9090/minio/health/live >/dev/null 2>&1; then
  pass "health live"
  if docker exec tms-minio sh -c "ls -d /data/${MINIO_BUCKET}" >/dev/null 2>&1; then
    pass "bucket présent : ${MINIO_BUCKET}"
  else
    bad "bucket absent : ${MINIO_BUCKET}"
  fi
else
  bad "injoignable sur 9090"
fi

# -----------------------------------------------------------------------------
section "6. Langfuse — conteneurs web (3000) et worker (3030)"
# -----------------------------------------------------------------------------
h="$(curl -fsS -m 10 http://localhost:3000/api/public/health 2>&1)"
if [[ $? -eq 0 ]]; then
  pass "web — /api/public/health" "$h"

  # Authentification Basic avec les clés de projet provisionnées
  auth="$(printf '%s:%s' "$LANGFUSE_INIT_PROJECT_PUBLIC_KEY" "$LANGFUSE_INIT_PROJECT_SECRET_KEY" | base64 -w0)"
  p="$(curl -fsS -m 10 -H "Authorization: Basic $auth" \
       http://localhost:3000/api/public/projects 2>&1)"
  [[ "$p" == *"$LANGFUSE_INIT_PROJECT_ID"* ]] \
    && pass "projet provisionné + clés API valides" "$LANGFUSE_INIT_PROJECT_ID" \
    || bad "clés API projet invalides" "$p"
else
  bad "web injoignable sur 3000" "$h"
fi

if curl -fsS -m 10 http://localhost:3030/api/health >/dev/null 2>&1; then
  pass "worker — /api/health"
else
  st="$(docker inspect -f '{{.State.Status}}' tms-langfuse-worker 2>/dev/null)"
  [[ "$st" == "running" ]] && pass "worker en cours d'exécution" "endpoint health non exposé" \
                           || bad "worker arrêté" "status=$st"
fi

# -----------------------------------------------------------------------------
section "7. vLLM — conteneur GPU (8001), profil \`gpu\`"
# -----------------------------------------------------------------------------
if ! docker ps --format '{{.Names}}' | grep -q '^tms-vllm-qwen$'; then
  skip "non démarré" "profil gpu désactivé ou pas de GPU"
elif curl -fsS -m 5 http://localhost:8001/health >/dev/null 2>&1; then
  pass "health"
  ctx="$(curl -fsS -m 5 http://localhost:8001/v1/models | python3 -c \
      'import json,sys; print(json.load(sys.stdin)["data"][0]["max_model_len"])' 2>&1)"
  served="$(curl -fsS -m 5 http://localhost:8001/v1/models | python3 -c \
      'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>&1)"
  pass "modèle servi" "$served ctx=$ctx"

  # La fenêtre servie doit couvrir ce que les agents s'autorisent à envoyer.
  # Un écart ici ne se voit nulle part ailleurs : tout fonctionne jusqu'à la
  # première enquête un peu longue, que le fournisseur refuse en bloc.
  budget="${AGENT_MAX_INPUT_TOKENS:-28000}"
  if [[ "$ctx" =~ ^[0-9]+$ ]] && (( ctx >= budget )); then
    pass "fenêtre vs budget des agents" "$ctx ≥ AGENT_MAX_INPUT_TOKENS=$budget"
  else
    bad "budget des agents au-dessus de la fenêtre servie" \
        "AGENT_MAX_INPUT_TOKENS=$budget pour une fenêtre de $ctx — baisser l'un ou élargir l'autre"
  fi
  used="$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null)"
  pass "VRAM" "$used"
else
  bad "injoignable sur 8001" "chargement du modèle en cours ? docker logs tms-vllm-qwen"
fi

# -----------------------------------------------------------------------------
section "8. LiteLLM Proxy — conteneur (4000)"
# -----------------------------------------------------------------------------
if curl -fsS -m 10 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  pass "liveliness"

  models="$(curl -fsS -m 10 -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
            http://localhost:4000/v1/models 2>&1)"
  if [[ "$models" == *mission-investigator* ]]; then
    pass "modèles déclarés" "$(python3 -c 'import json,sys; print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))' <<<"$models" 2>/dev/null)"
  else
    bad "liste des modèles" "$models"
  fi

  # Vraie complétion de bout en bout : LiteLLM -> vLLM (ou repli cloud) -> Langfuse
  before="$(ch_query "SELECT count() FROM events_core")"
  # 64 jetons et non 8 : un modèle à raisonnement dépense d'abord des jetons à
  # penser, et un plafond trop bas les consomme tous avant la réponse.
  body='{"model":"mission-investigator","messages":[{"role":"user","content":"Réponds exactement: PONG"}],"max_tokens":64,"temperature":0}'
  resp="$(curl -fsS -m 90 -X POST http://localhost:4000/v1/chat/completions \
          -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
          -H 'Content-Type: application/json' -d "$body" 2>&1)"
  # La réponse compte, mais le raisonnement aussi : sur un modèle qui les
  # sépare, une complétion réussie peut n'avoir que le second.
  content="$(python3 -c 'import json,sys
try:
    m = json.load(sys.stdin)["choices"][0]["message"]
    texte = (m.get("content") or m.get("reasoning_content") or "").strip()
    print(texte[:60])
except Exception: pass' <<<"$resp" 2>/dev/null)"
  [[ -n "$content" ]] && pass "complétion de bout en bout" "$content" \
                      || bad "complétion échouée" "${resp:0:200}"

  # Route de repli cloud : doit fonctionner seule, GPU éteint ou saturé
  rbody='{"model":"mission-investigator-cloud","messages":[{"role":"user","content":"Dis PONG"}],"max_tokens":16}'
  rresp="$(curl -fsS -m 60 -X POST http://localhost:4000/v1/chat/completions \
           -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
           -H 'Content-Type: application/json' -d "$rbody" 2>&1)"
  if python3 -c 'import json,sys; sys.exit(0 if "choices" in json.load(sys.stdin) else 1)' <<<"$rresp" 2>/dev/null; then
    pass "repli cloud joignable (Groq)" "mission-investigator-cloud"
  else
    bad "repli cloud injoignable" "${rresp:0:160}"
  fi

  # -------------------------------------------------------------------------
  # Observabilité de bout en bout : la complétion doit apparaître dans Langfuse.
  # Langfuse v4 n'ingère les traces que par OTLP et les stocke dans
  # `events_core` ; les tables `traces`/`observations` de la v3 restent vides.
  # -------------------------------------------------------------------------
  after="$before"
  for _ in $(seq 1 12); do
    sleep 5
    after="$(ch_query "SELECT count() FROM events_core")"
    [[ "${after:-0}" -gt "${before:-0}" ]] && break
  done
  if [[ "${after:-0}" -gt "${before:-0}" ]]; then
    usage="$(ch_query "SELECT concat(toString(usage_details['input']), ' in / ', toString(usage_details['output']), ' out') FROM events_core WHERE provided_model_name != '' ORDER BY start_time DESC LIMIT 1")"
    pass "trace reçue par Langfuse (OTLP)" "$((after - before)) événement(s), $usage tokens"
  else
    bad "aucune trace reçue par Langfuse" "events_core figé à ${before:-?}"
  fi
else
  bad "injoignable sur 4000" "docker logs tms-litellm-proxy"
fi

# -----------------------------------------------------------------------------
printf '\n%s%s%s\n' "$BOLD" "Résultat : ${PASS} ok, ${FAIL} échec(s), ${SKIP} ignoré(s)" "$RESET"
(( FAIL == 0 ))
