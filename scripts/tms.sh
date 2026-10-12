#!/usr/bin/env bash
# =============================================================================
#  tms.sh — pilote le TMS simulé (x_tms) depuis la racine du projet
# =============================================================================
#  x_tms est un projet distinct, avec son propre environnement Python et ses
#  propres commandes. Ce lanceur évite d'avoir à changer de répertoire et à se
#  souvenir de l'invocation exacte, et il place `status` et `reset` à portée de
#  main — les deux commandes qui règlent le problème le plus courant en
#  démonstration : une mission « zombie » laissée par une simulation
#  interrompue, et une horloge simulée restée en avance.
#
#  Usage :
#      scripts/tms.sh status                 # ce que contient le TMS, maintenant
#      scripts/tms.sh reset                  # vide missions, événements, positions, horloge
#      scripts/tms.sh serve                   # démarre l'API (port 8000)
#      scripts/tms.sh client                  # démarre l'interface web
#      scripts/tms.sh scenarios               # liste les scénarios
#      scripts/tms.sh start [options…]        # start-missions, options transmises
#      scripts/tms.sh demo <scénario>         # reset + départ juste avant l'anomalie
#      scripts/tms.sh demo all [options…]     # les quatre missions en même temps
#
#  `x_tms` doit être extrait dans un dépôt séparé à terme : son chemin est donc
#  réglable par la variable d'environnement X_TMS_PATH.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
X_TMS_PATH="${X_TMS_PATH:-$ROOT_DIR/x_tms}"
SERVER_DIR="$X_TMS_PATH/server"
CLIENT_DIR="$X_TMS_PATH/client"
TMS_URL="${TMS_BASE_URL:-http://127.0.0.1:8000}"

RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; DIM=$'\e[2m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
info() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
ok()   { printf '%s  ok%s  %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s warn%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '%s fail%s %s\n' "$RED" "$RESET" "$*"; }

[[ -d "$SERVER_DIR" ]] || { fail "TMS simulé introuvable : $SERVER_DIR (régler X_TMS_PATH)"; exit 1; }

# `env -u PYTHONPATH` : l'installation ROS de ce poste place son site-packages
# dans le PYTHONPATH global, ce qui perturbe l'environnement de x_tms.
# `python -m tms.cli` et non `tms` : son pyproject.toml ne déclare pas de
# [project.scripts], donc la commande `tms` n'est pas installée.
#  `-u VIRTUAL_ENV` : notre venv est actif, et uv avertirait à chaque appel
#  qu'il ne correspond pas à celui de x_tms. x_tms a le sien, c'est voulu.
tms_cli() {
  (cd "$SERVER_DIR" && env -u PYTHONPATH -u VIRTUAL_ENV uv run --project . python -m tms.cli "$@")
}

# --- Départs calés juste avant chaque anomalie -------------------------------
#  T+0 est le début de la mission, pas le départ du camion : 10 min d'approche
#  puis 32 min de chargement, le transit ne commence qu'à T+42. Les valeurs de
#  saut tiennent compte de ce décalage et du seuil de la règle concernée.

# Les quatre scénarios livrés par x_tms. `incident` est optionnel côté x_tms —
# il est inclus ici parce que c'est précisément celui qu'on veut voir déclencher.
ALL_SCENARIOS=(nominal deviation immobilization incident)

demo_args() {
  case "$1" in
    deviation)      echo "-s deviation --skip-minutes 50" ;;       # écart dès T+45, règle à 20 min
    immobilization) echo "-s immobilization --skip-minutes 150" ;; # arrêt dès T+60, règle à 2 h
    incident)       echo "-s incident --skip-minutes 50" ;;        # message critique à T+55
    nominal)        echo "-s nominal" ;;                           # aucune anomalie à atteindre
    all)
      # Les quatre missions en parallèle, avec **un seul** saut d'horloge :
      # l'horloge simulée est commune à toute la simulation, et chaque
      # scénario a son anomalie à un instant différent (T+45, T+55, T+60).
      # Sauter 40 minutes place le départ juste avant la première sans
      # franchir les suivantes — un saut de 150, comme pour l'immobilisation
      # seule, aurait fait manquer l'écart et l'incident, déjà passés.
      local flags=()
      local scenario
      for scenario in "${ALL_SCENARIOS[@]}"; do flags+=("-s" "$scenario"); done
      echo "${flags[*]} --skip-minutes 40"
      ;;
    *) return 1 ;;
  esac
}

# Vitesse par défaut d'une démonstration. Trois pour une mission seule : le
# mouvement reste observable dans l'interface. Dix pour « toutes », parce que
# la dernière alerte attendue — l'immobilisation, à T+180 — demanderait sinon
# trois quarts d'heure de temps réel.
demo_speed() {
  if [[ "$1" == "all" ]]; then echo 10; else echo 3; fi
}

# Ce qu'il faut attendre, et quand. Affiché avant le départ parce qu'une
# démonstration où l'on ne sait pas quoi guetter ressemble à une démonstration
# qui ne marche pas. Les instants sont **mesurés** sur des runs complets, pas
# déduits des seuils : l'écart met quelques minutes à franchir la tolérance, et
# l'âge du dernier événement courait avant l'arrêt.
demo_plan() {
  local scenario="$1" speed="$2"
  local -a lignes=()
  local -a notes=()
  case "$scenario" in
    deviation|all)
      lignes+=("deviation_persistent|63|écart > 5 km pendant 20 min")
      # Observé sur un vrai run : le véhicule regagne sa route vers T+75, la
      # règle cesse d'être vraie et la clôture automatique s'applique (F13).
      # Sans cet avertissement, qui consulte la liste dix minutes plus tard
      # voit « resolved » et croit avoir manqué l'alerte.
      notes+=("l'écart se referme vers T+75 : l'alerte passera d'elle-même en 'resolved' (clôture automatique)")
      ;;
  esac
  case "$scenario" in
    incident|all)       lignes+=("critical_incident|55|message critique du chauffeur") ;;
  esac
  case "$scenario" in
    immobilization|all)
      # T+162 et non T+180 : la règle compte l'âge du **dernier événement de
      # mission**, et le dernier est le départ du chargement à T+42 — le
      # transit n'en émet aucun. L'arrêt commence à T+60, mais le compteur,
      # lui, courait déjà. Mesuré sur un run complet : alerte à T+162.
      lignes+=("immobilization_anomaly|162|véhicule arrêté, aucun événement depuis 2 h")
      ;;
  esac
  if [[ ${#lignes[@]} -eq 0 ]]; then
    info "Aucune anomalie attendue sur ce scénario : c'est la mission nominale."
    return
  fi

  info "Alertes attendues (T = création de la mission, horloge simulée)"
  local ligne regle instant motif minutes reel
  for ligne in "${lignes[@]}"; do
    IFS='|' read -r regle instant motif <<<"$ligne"
    # Temps réel restant = (instant de l'alerte - minutes déjà sautées) / vitesse.
    minutes=$(awk -v t="$instant" -v skip="$DEMO_SKIPPED" 'BEGIN { printf "%.0f", t - skip }')
    reel=$(awk -v m="$minutes" -v s="$speed" \
      'BEGIN { if (s <= 0) s = 1; printf "%.0f", (m < 0 ? 0 : m) / s }')
    printf '  %sT+%-4s%s %-24s %s%s(~%s min réelles)%s\n' \
      "$DIM" "$instant" "$RESET" "$regle" "$motif " "$DIM" "$reel" "$RESET"
  done
  # L'écart ne dure que 30 minutes simulées et la règle demande 20 minutes de
  # persistance : la fenêtre pendant laquelle l'alerte peut naître est étroite,
  # et un worker démarré après coup ne verra rien.
  warn "Démarrer le worker de surveillance *avant* ces échéances : les fenêtres sont étroites"
  local note
  for note in ${notes[@]+"${notes[@]}"}; do
    printf '  %s%s%s\n' "$DIM" "$note" "$RESET"
  done
}

# Une option est-elle déjà fournie par l'appelant ? Sans ce contrôle, nos
# valeurs par défaut et les siennes se retrouveraient toutes deux sur la ligne
# de commande, et c'est le hasard de l'analyseur qui trancherait.
has_option() {
  local wanted="$1"; shift
  local argument
  for argument in "$@"; do
    [[ "$argument" == "$wanted" || "$argument" == "$wanted="* ]] && return 0
  done
  return 1
}

# --- Une simulation tourne-t-elle encore ? -----------------------------------
#  Remettre la base à zéro sous une simulation vivante supprime ses missions
#  sous ses pieds : elle plante, et en sortant elle réancre l'horloge simulée
#  sur l'heure qu'elle avait atteinte. On se retrouve alors avec une base
#  vide *et* une horloge en avance — exactement l'état qu'on cherchait à fuir.
simulation_running() {
  if pgrep -f "tms.cli start-missions" >/dev/null 2>&1; then
    return 0
  fi
  curl -fsS -m 3 "$TMS_URL/v1/health" 2>/dev/null | grep -q '"running":[[:space:]]*true'
}

stop_simulations() {
  if pgrep -f "tms.cli start-missions" >/dev/null 2>&1; then
    info "Arrêt des simulations en cours"
    # SIGINT et non SIGKILL : le simulateur annule proprement ses missions
    # non terminées, ce qui évite précisément de laisser des zombies.
    pkill -INT -f "tms.cli start-missions" || true
    sleep 2
  fi
}

verify_clean() {
  local db="$SERVER_DIR/data/tms.db"
  if [[ ! -f "$db" ]]; then
    ok "TMS vide : rien ne sera visible tant qu'aucune mission n'est démarrée"
    return 0
  fi
  # Vérifier plutôt qu'annoncer : c'est précisément ce dont on doute.
  local verdict
  verdict="$(env -u PYTHONPATH python3 -c "
import sqlite3, sys
c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)
print(c.execute('select count(*) from missions').fetchone()[0],
      c.execute('select count(*) from simulation_clock').fetchone()[0])
" "$db")"
  read -r missions clock <<<"$verdict"
  if [[ "$missions" == "0" && "$clock" == "0" ]]; then
    ok "TMS vide, horloge à l'heure réelle : rien ne sera visible sans démarrer une mission"
  else
    warn "après remise à zéro : $missions mission(s), $clock ligne(s) d'horloge — relancer 'status'"
  fi
}

status() {
  info "TMS simulé — $TMS_URL"

  if ! curl -fsS -m 3 "$TMS_URL/v1/health" >/dev/null 2>&1; then
    warn "API arrêtée — 'scripts/tms.sh serve' pour la démarrer"
    local db="$SERVER_DIR/data/tms.db"
    [[ -f "$db" ]] || { warn "aucune base : elle sera créée au démarrage"; return 0; }
    # Lecture seule : l'état est consultable même API éteinte, ce qui est
    # précisément le moment où l'on cherche à savoir ce qui traîne en base.
    env -u PYTHONPATH python3 - "$db" <<'PY'
import sqlite3, sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
missions = dict(connection.execute("select status, count(*) from missions group by status"))
total = sum(missions.values())
print(f"  missions en base : {total}" + (f" ({missions})" if missions else " (aucune)"))
clock = connection.execute("select sim_anchor, speed, running, run_id from simulation_clock").fetchone()
print("  horloge simulée  :", "heure réelle" if clock is None else clock)
PY
    return 0
  fi

  env -u PYTHONPATH python3 - "$TMS_URL" <<'PY'
import json, sys, urllib.request

base = sys.argv[1].rstrip("/") + "/v1"


def get(path):
    with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
        return json.load(response)


from datetime import UTC, datetime  # noqa: E402

health = get("/health")
tms_time = datetime.fromisoformat(health["time"].replace("Z", "+00:00"))
drift = (tms_time - datetime.now(UTC)).total_seconds() / 60
simulation = health["simulation"]

print(f"  heure du TMS     : {tms_time.isoformat()}  (écart réel : {drift:+.0f} min)")
print(f"  simulation       : running={simulation['running']} speed=x{simulation['speed']:g} "
      f"run={simulation['run_id'] or '-'}")

missions = get("/missions?limit=200")["data"]
by_status: dict[str, int] = {}
for mission in missions:
    by_status[mission["status"]] = by_status.get(mission["status"], 0) + 1
print(f"  missions         : {len(missions)}" + (f"  {by_status}" if by_status else "  (aucune)"))

actives = [m for m in missions if m["status"] in ("planned", "in_progress")]
for mission in actives:
    deviation = get(f"/missions/{mission['id']}/deviation")
    eta = get(f"/missions/{mission['id']}/eta")
    print(
        f"     {mission['reference']:18} {mission['status']:12} "
        f"écart={deviation.get('current_offset_km') or 0:5.2f} km "
        f"depuis={deviation.get('duration_minutes') or 0:5.1f} min "
        f"retard={eta.get('delay_minutes') or 0:+7.1f} min"
    )

# Les deux symptômes d'une simulation interrompue brutalement : une mission
# restée en cours sans processus pour l'avancer, et une horloge en avance.
if abs(drift) > 5 and not simulation["running"]:
    print("\n  ⚠ horloge simulée décalée alors qu'aucune simulation ne tourne")
    print("    -> scripts/tms.sh reset")
if actives and not simulation["running"]:
    print("\n  ⚠ mission(s) en cours sans simulation active : vraisemblablement des zombies")
    print("    -> scripts/tms.sh reset")
PY
}

command="${1:-status}"
shift || true

case "$command" in
  status)
    status
    ;;
  reset)
    if simulation_running; then
      warn "Une simulation tourne encore : la remise à zéro la ferait planter,"
      warn "et l'horloge simulée resterait en avance."
      if [[ "${1:-}" == "-f" || "${1:-}" == "--force" ]]; then
        stop_simulations
      else
        fail "Arrêter la simulation (Ctrl+C) puis relancer, ou 'scripts/tms.sh reset -f'"
        exit 1
      fi
    fi
    info "Suppression des missions, événements, positions et remise de l'horloge"
    tms_cli reset -y
    verify_clean
    ;;
  serve)
    info "API du TMS sur $TMS_URL — doc : $TMS_URL/v1/docs"
    # `uvicorn` directement : la commande `tms` n'est pas installée.
    (cd "$SERVER_DIR" && env -u PYTHONPATH -u VIRTUAL_ENV uv run --project . \
      python -m uvicorn tms.api.app:app --host 127.0.0.1 --port 8000 "$@")
    ;;
  client)
    [[ -d "$CLIENT_DIR" ]] || { fail "Interface introuvable : $CLIENT_DIR"; exit 1; }
    [[ -d "$CLIENT_DIR/node_modules" ]] || (cd "$CLIENT_DIR" && npm install)
    info "Interface du TMS — l'URL exacte s'affiche ci-dessous (5173 si libre)"
    (cd "$CLIENT_DIR" && npm run dev)
    ;;
  scenarios)
    tms_cli scenarios
    ;;
  start)
    tms_cli start-missions "$@"
    ;;
  demo)
    scenario="${1:-deviation}"
    shift || true
    if ! args="$(demo_args "$scenario")"; then
      fail "Scénario inconnu : $scenario (nominal, deviation, immobilization, incident, all)"
      exit 1
    fi

    # Minutes sautées, relues dans les arguments du scénario : `demo_plan` en a
    # besoin pour annoncer un délai réel juste, et les écrire deux fois les
    # ferait divergir au premier ajustement de seuil.
    DEMO_SKIPPED="$(printf '%s\n' "$args" | grep -oE '\-\-skip-minutes [0-9]+' | awk '{print $2}')"
    DEMO_SKIPPED="${DEMO_SKIPPED:-0}"

    # Options de l'appelant, complétées par nos valeurs par défaut — jamais
    # doublées : deux `--speed` sur la même ligne laisseraient l'analyseur
    # trancher, et c'est à l'appelant de le faire.
    extra=("$@")
    speed="$(demo_speed "$scenario")"
    for ((index = 0; index < ${#extra[@]}; index++)); do
      case "${extra[index]}" in
        --speed)   speed="${extra[index + 1]:-$speed}" ;;
        --speed=*) speed="${extra[index]#*=}" ;;
      esac
    done
    has_option --speed "${extra[@]:-}" || extra+=(--speed "$speed")
    has_option --tick-seconds "${extra[@]:-}" || extra+=(--tick-seconds 10)

    if [[ "$scenario" == "all" ]]; then
      info "Démonstration complète — ${#ALL_SCENARIOS[@]} missions en parallèle : ${ALL_SCENARIOS[*]}"
    else
      info "Démonstration '$scenario' — base remise à zéro, départ juste avant l'anomalie"
    fi
    stop_simulations
    tms_cli reset -y
    verify_clean
    demo_plan "$scenario" "$speed"
    info "Ctrl+C arrête la simulation proprement (ses missions sont annulées, pas laissées zombies)"
    # shellcheck disable=SC2086
    tms_cli start-missions $args "${extra[@]}" -v
    ;;
  *)
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
