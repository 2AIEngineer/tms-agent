"""Outils en ligne de commande pour raccorder un TMS.

    uv run python -m services.mcp_server.cli list
    uv run python -m services.mcp_server.cli validate chemin/vers/dialecte.yaml
    uv run python -m services.mcp_server.cli report fleetlink

`validate` est la commande à lancer en écrivant un dialecte : elle dit ce qui
manque, avec le nom de la clé à corriger. `report` produit le rapport de
capacité — le document à mettre sur la table avec l'éditeur du TMS.
"""

import sys
from pathlib import Path

from services.monitoring.config import get_settings as monitoring_settings
from services.monitoring.rules.loader import RuleSet, load_rules

from .capability import Verdict, build_report
from .connectors.dialect import available_dialects, load_dialect, load_named_dialect

USAGE = __doc__ or ""


def _ruleset() -> RuleSet:
    """Règles livrées, pour mesurer l'effet d'une capacité absente."""
    return load_rules(monitoring_settings().rules_directory)


def command_list() -> int:
    names = available_dialects()
    if not names:
        print("Aucun dialecte livré.")
        return 0
    print("Dialectes disponibles :")
    for name in names:
        dialect = load_named_dialect(name)
        print(f"  {name:12} {dialect.label}")
    return 0


def command_validate(target: str) -> int:
    """Valide un dialecte, qu'il soit livré ou en cours d'écriture."""
    path = Path(target)
    try:
        dialect = load_dialect(path) if path.is_file() else load_named_dialect(target)
    except Exception as error:
        print(f"Dialecte refusé : {error}", file=sys.stderr)
        return 1

    print(f"Dialecte valide : {dialect.tms_id} — {dialect.label}")
    absent = [name for name, spec in dialect.operations.items() if spec.unsupported]
    if absent:
        print("Opérations déclarées absentes : " + ", ".join(sorted(o.value for o in absent)))
        print("Lancer 'report' pour connaître les règles que cela rend inopérantes.")
    return 0


def command_report(name: str) -> int:
    try:
        dialect = load_named_dialect(name)
    except Exception as error:
        print(f"Dialecte introuvable : {error}", file=sys.stderr)
        return 1

    report = build_report(dialect, _ruleset())
    print(report.render())
    # Un TMS inexploitable est une erreur de sortie : utilisable dans un script
    # de raccordement pour interrompre un déploiement voué à ne rien détecter.
    return 1 if report.verdict is Verdict.BLOCKED else 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    command = arguments.pop(0) if arguments else "list"

    if command == "list":
        return command_list()
    if command == "validate" and arguments:
        return command_validate(arguments[0])
    if command == "report" and arguments:
        return command_report(arguments[0])

    print(USAGE.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
