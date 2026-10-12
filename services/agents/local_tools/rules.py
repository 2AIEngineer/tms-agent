"""Réévaluation des règles sur la mission (F21).

L'enquêteur reçoit l'alerte telle qu'elle a été créée, parfois plusieurs
minutes plus tôt. Cet outil répond à une question qu'aucune autre lecture ne
tranche : **la condition est-elle encore vraie ?**

C'est ce qui permet de distinguer trois situations que l'opérateur ne doit pas
confondre :

  - la règle déclenche toujours — la situation dure ;
  - elle ne déclenche plus — la situation s'est résorbée d'elle-même, et la
    clôture automatique va suivre (F13) ;
  - elle n'a jamais pu déclencher sur ces données — soupçon de fausse alerte,
    que l'évaluation finale peut alors signaler (F22).

Le moteur est appelé **tel quel**, celui-là même qui a créé l'alerte : aucun
LLM, aucun réseau, aucune base. Le réimplémenter ici pour « simplifier »
produirait deux vérités divergentes, et c'est l'alerte qui perdrait sa valeur.
"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from services.monitoring.rules.engine import evaluate_state_rules
from services.monitoring.rules.loader import RuleRepository
from shared.mapped_dto import InvestigationRequest, MissionSnapshot, RuleEvaluation

from ..integrations.mcp_client import AgentMcpClient

logger = logging.getLogger(__name__)

RULES_TOOL_NAME = "evaluate_rules"


class EvaluateArguments(BaseModel):
    """Aucun paramètre : la mission et les règles ne se choisissent pas.

    Laisser le modèle désigner une règle l'inviterait à ne vérifier que celle
    qui l'arrange. Toutes les règles d'état sont évaluées, et le résultat les
    rend toutes.
    """

    model_config = ConfigDict(extra="forbid")


def rules_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": RULES_TOOL_NAME,
            "description": (
                "Réévalue les règles métier sur l'état actuel de la mission et dit "
                "lesquelles déclenchent encore. À employer pour savoir si la "
                "situation dure, s'est résorbée, ou si l'alerte semble infondée."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


class RuleCheckTool:
    """Rejoue le moteur de règles sur l'instantané courant de la mission."""

    name = RULES_TOOL_NAME

    def __init__(self, rules: RuleRepository, mcp: AgentMcpClient) -> None:
        self._rules = rules
        self._mcp = mcp

    @staticmethod
    def definition() -> dict[str, Any]:
        return rules_tool_definition()

    async def invoke(self, request: InvestigationRequest, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            EvaluateArguments.model_validate(arguments or {})
        except ValidationError:
            # Un paramètre inventé n'empêche pas l'évaluation : elle ne dépend
            # d'aucun argument. On le signale sans échouer.
            logger.info("Arguments ignorés pour %s : %s", RULES_TOOL_NAME, arguments)

        snapshot = await self._snapshot(request)
        if snapshot is None:
            return {
                "ok": True,
                "evaluated": False,
                "note": (
                    "Mission plus lisible : les règles ne peuvent pas être réévaluées. "
                    "Ne rien conclure de cette absence."
                ),
            }

        # Les règles sont relues à chaud : un seuil modifié depuis la détection
        # doit s'appliquer ici aussi, sinon l'enquête juge sur une règle qui
        # n'existe plus (F8).
        ruleset = self._rules.reload_if_changed()
        evaluations = evaluate_state_rules(ruleset.rules, snapshot)

        matched = [_render(item) for item in evaluations if item.matched]
        unmatched = [_render(item) for item in evaluations if not item.matched]
        return {
            "ok": True,
            "evaluated": True,
            "tms_time": snapshot.observed_at.isoformat(),
            "snapshot_complete": snapshot.snapshot_complete,
            "rule_of_this_alert": request.alert.rule_id,
            # Nommé explicitement : c'est la réponse attendue, et la chercher
            # dans deux listes est précisément ce qu'un modèle rate.
            "this_rule_still_matches": any(
                item.rule_id == request.alert.rule_id for item in evaluations if item.matched
            ),
            "matched": matched,
            "not_matched": [item["rule_id"] for item in unmatched],
        }

    async def _snapshot(self, request: InvestigationRequest) -> MissionSnapshot | None:
        result = await self._mcp.call(
            "get_mission_snapshot",
            {"tms_id": request.tms_id, "mission_id": request.mission_id},
        )
        raw = result.payload.get("snapshot") if result.ok else None
        if not isinstance(raw, dict):
            return None
        try:
            return MissionSnapshot.model_validate(raw)
        except Exception:
            logger.warning("Instantané non conforme : réévaluation impossible")
            return None


def _render(evaluation: RuleEvaluation) -> dict[str, Any]:
    """Réduit une évaluation à ce qui éclaire l'enquête.

    Le message rendu et les destinataires sont omis : l'alerte est déjà créée
    et notifiée, les répéter n'apporterait rien et occuperait la fenêtre.
    """
    return {
        "rule_id": evaluation.rule_id,
        "rule_version": evaluation.rule_version,
        "severity": evaluation.severity.value if evaluation.severity else None,
        "category": evaluation.category,
        "observed_data": evaluation.observed_data,
    }
