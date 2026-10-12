"""Outil MCP de découverte des TMS raccordés.

Corollaire indispensable d'un `tms_id` obligatoire : un appelant doit pouvoir
savoir quels TMS existent sans connaître la configuration du serveur. C'est le
serveur qui détient les connexions, et le worker de surveillance comme l'agent
les découvrent ici.

Le rapport de capacité de chaque TMS est joint, parce qu'il change ce qu'un
appelant peut en attendre : un TMS qui ne fournit pas l'écart à l'itinéraire ne
déclenchera jamais les règles de dérive, et mieux vaut le savoir avant de
conclure à l'absence d'anomalie.
"""

from typing import Any

from fastmcp import FastMCP

from services.monitoring.rules.loader import load_rules

from ..capability import build_report
from ..context import McpContext, success, unexpected_error


def register(mcp: FastMCP, context: McpContext) -> None:
    @mcp.tool
    async def list_tms() -> dict[str, Any]:
        """Liste les TMS raccordés, avec leurs capacités.

        Tous les autres outils exigent un `tms_id` : celui-ci dit lesquels sont
        valides. Le worker de surveillance s'en sert pour lancer une boucle par
        TMS, l'agent pour savoir dans quel périmètre il enquête.

        Pour chaque TMS :
          - `verdict` vaut `ok`, `degraded` ou `blocked`. `degraded` signifie
            que certaines règles ne pourront jamais déclencher ;
          - `inoperative_rules` les nomme explicitement ;
          - `unsupported_operations` liste ce que ce TMS ne sait pas rendre.

        Ne pas conclure à l'absence d'anomalie sur un TMS dégradé sans avoir lu
        quelles règles y sont inopérantes.
        """
        try:
            # Les règles sont relues à chaque appel : elles sont rechargeables à
            # chaud, et un rapport calculé une fois pour toutes vieillirait.
            ruleset = load_rules(context.settings.rules_directory)
            entries = []
            for connection in context.connections:
                report = build_report(connection.dialect, ruleset)
                entries.append(
                    {
                        "tms_id": connection.tms_id,
                        "label": connection.label,
                        "dialect": connection.dialect.tms_id,
                        "verdict": report.verdict.value,
                        "unsupported_operations": list(report.unsupported_operations),
                        "inoperative_rules": [
                            {
                                "rule_id": rule.rule_id,
                                "severity": rule.severity,
                                "unavailable_fields": list(rule.unavailable_fields),
                            }
                            for rule in report.inoperative_rules
                        ],
                    }
                )
        except Exception as exception:
            return unexpected_error("list_tms", exception)

        return success({"count": len(entries), "tms": entries})
