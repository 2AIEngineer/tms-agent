#!/usr/bin/env bash
# =============================================================================
#  test.sh — lance la suite de tests dans un environnement propre
# =============================================================================
#  `env -u PYTHONPATH` n'est pas une précaution de style : l'installation ROS
#  Jazzy de ce poste place son `site-packages` dans le PYTHONPATH global, et
#  pytest y découvre automatiquement les plugins `launch_testing` de ROS, dont
#  les dépendances sont absentes de notre venv. La collecte échoue alors avant
#  d'avoir exécuté le moindre test.
#
#  Usage : scripts/test.sh [arguments pytest]
#          scripts/test.sh tests/unit/monitoring -k operateur
# =============================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec env -u PYTHONPATH uv run pytest "$@"
