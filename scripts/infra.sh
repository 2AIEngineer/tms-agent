#!/usr/bin/env bash
# =============================================================================
#  infra.sh — pilote la stack d'infrastructure de développement
# =============================================================================
#  Usage :
#      scripts/infra.sh up        # démarre (profil gpu auto-détecté)
#      scripts/infra.sh up --no-gpu
#      scripts/infra.sh down      # arrête et supprime les conteneurs
#      scripts/infra.sh restart
#      scripts/infra.sh status
#      scripts/infra.sh migrate   # applique les migrations Alembic seules
#      scripts/infra.sh logs [service]
#      scripts/infra.sh check     # vérifie la santé de tous les services
#      scripts/infra.sh reset     # down + suppression des volumes (destructif)
#
#  PostgreSQL et Redis sont natifs sur l'hôte : ce script ne les gère pas, il
#  se contente de vérifier qu'ils répondent avant de démarrer les conteneurs.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="$ROOT_DIR/infra"
ENV_FILE="$ROOT_DIR/.env"

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
info()  { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()    { printf '%s  ok%s  %s\n' "$GREEN" "$RESET" "$*"; }
warn()  { printf '%s warn%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail()  { printf '%s fail%s %s\n' "$RED" "$RESET" "$*"; }

[[ -f "$ENV_FILE" ]] || { fail "Fichier manquant : $ENV_FILE (copier .env.example)"; exit 1; }

# shellcheck disable=SC2046
set -a; source "$ENV_FILE"; set +a

compose() { docker compose --env-file "$ENV_FILE" -f "$INFRA_DIR/docker-compose.yml" "$@"; }

# --- Prérequis hôte : les deux bases natives doivent répondre -----------------
require_host_services() {
  info "Vérification des services natifs de l'hôte"
  if PGPASSWORD="$POSTGRES_PASSWORD" pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -q; then
    ok "PostgreSQL $POSTGRES_HOST:$POSTGRES_PORT"
  else
    fail "PostgreSQL injoignable sur $POSTGRES_HOST:$POSTGRES_PORT — 'sudo systemctl start postgresql'"
    exit 1
  fi
  if [[ "$(redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null)" == "PONG" ]]; then
    ok "Redis $REDIS_HOST:$REDIS_PORT"
  else
    fail "Redis injoignable sur $REDIS_HOST:$REDIS_PORT — 'sudo systemctl start redis-server'"
    exit 1
  fi
}

# --- Les bases dédiées doivent exister avant le démarrage de Langfuse ---------
require_databases() {
  local missing=()
  for db in "$POSTGRES_DB" tms_agent_test_db langfuse_db litellm_db; do
    if ! PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
         -U "$POSTGRES_USER" -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$db"; then
      missing+=("$db")
    fi
  done
  if ((${#missing[@]})); then
    warn "Bases absentes : ${missing[*]}"
    for db in "${missing[@]}"; do
      PGPASSWORD="$POSTGRES_PASSWORD" createdb -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" "$db" && ok "base créée : $db"
    done
  else
    ok "Bases présentes : $POSTGRES_DB, tms_agent_test_db, langfuse_db, litellm_db"
  fi
}

# --- Migrations du schéma applicatif -----------------------------------------
#  Appliquées au démarrage : un schéma en retard se traduirait par des erreurs
#  SQL au premier cycle de surveillance, bien plus difficiles à diagnostiquer.
migrate_databases() {
  info "Migrations du schéma orchestrator"
  for url in "$DATABASE_URL" "$TEST_DATABASE_URL"; do
    if DATABASE_URL="$url" env -u PYTHONPATH uv run alembic -c database/alembic.ini upgrade head >/dev/null 2>&1; then
      ok "à jour : ${url##*/}"
    else
      fail "migration en échec sur ${url##*/} — relancer sans -q pour le détail"
      return 1
    fi
  done
}

# --- Profil GPU : activé si un GPU NVIDIA est visible ------------------------
gpu_profile_args() {
  [[ "${NO_GPU:-0}" == "1" ]] && return 0
  if nvidia-smi -L >/dev/null 2>&1; then
    printf -- '--profile gpu'
  else
    warn "Aucun GPU NVIDIA détecté : vLLM n'est pas démarré, LiteLLM basculera sur le repli cloud" >&2
  fi
}

cmd="${1:-status}"; shift || true
[[ "${1:-}" == "--no-gpu" ]] && { NO_GPU=1; shift; }

case "$cmd" in
  up)
    require_host_services
    require_databases
    migrate_databases
    info "Démarrage de la stack conteneurisée"
    # shellcheck disable=SC2046
    compose $(gpu_profile_args) up -d --remove-orphans "$@"
    echo
    compose --profile gpu ps
    echo
    info "Santé des services : scripts/infra.sh check"
    ;;
  down)
    info "Arrêt de la stack"
    compose --profile gpu down --remove-orphans "$@"
    ;;
  restart)
    "$0" down && "$0" up
    ;;
  migrate)
    migrate_databases
    ;;
  status)
    compose --profile gpu ps
    ;;
  logs)
    compose --profile gpu logs -f --tail=100 "$@"
    ;;
  check)
    exec "$ROOT_DIR/scripts/check_infra.sh"
    ;;
  reset)
    warn "Suppression des conteneurs ET des volumes (ClickHouse, MinIO)"
    read -r -p "Confirmer ? [y/N] " answer
    [[ "$answer" == "y" ]] || { info "Annulé"; exit 0; }
    compose --profile gpu down --remove-orphans --volumes
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
