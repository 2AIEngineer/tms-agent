"""Assistant en ligne de commande, pour l'exercer sans interface.

L'assistant est une bibliothèque appelée par l'API d'exploitation, qui n'existe
pas encore. Cette entrée en ligne de commande sert à l'éprouver contre
l'infrastructure réelle — vrai serveur MCP, vrai TMS, vrai modèle — et reste
utile ensuite pour diagnostiquer une réponse douteuse sans passer par le
frontend.

    uv run python -m services.agents.assistant.cli "Où en est msn_1 ?"
    uv run python -m services.agents.assistant.cli --mode report
    uv run python -m services.agents.assistant.cli --chat
    uv run python -m services.agents.assistant.cli --allow-actions "Clos l'alerte …"

`--allow-actions` est explicite à dessein : c'est la transposition en ligne de
commande de la permission que l'opérateur accorde demande par demande (F26).
"""

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Iterator

from shared.channels import ConsoleChannel
from shared.mapped_dto import (
    AssistantAnswer,
    AssistantMode,
    AssistantRequest,
    NotificationChannel,
)
from shared.utils.db import Database

from ..config import get_settings
from ..integrations.litellm_client import LiteLlmClient
from ..integrations.mcp_client import AgentMcpClient
from ..operations.driver_call import DriverCallService
from .actions import AssistantActions
from .service import AssistantService
from .tools import ASSISTANT_READ_TOOLS

logger = logging.getLogger(__name__)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assistant d'exploitation (F24-F26)")
    parser.add_argument("message", nargs="*", help="La question. Vide avec --chat pour une session.")
    parser.add_argument("--mode", choices=[mode.value for mode in AssistantMode], default="ask")
    parser.add_argument("--chat", action="store_true", help="Session interactive (mode chat).")
    parser.add_argument("--operator", default="cli_operator")
    parser.add_argument("--tms", action="append", default=[], help="Répétable. Vide = tous.")
    parser.add_argument("--mission", default=None)
    parser.add_argument("--alert", default=None)
    parser.add_argument("--conversation", default=None, help="Poursuivre une conversation existante.")
    parser.add_argument(
        "--allow-actions",
        action="store_true",
        help="Autorise clôture, appel et consignation pour cette session.",
    )
    return parser.parse_args(argv)


async def _run(options: argparse.Namespace) -> int:
    settings = get_settings()
    database = Database(settings.database_url)
    llm = LiteLlmClient(
        settings.litellm_base_url,
        settings.litellm_api_key,
        model=settings.model,
        temperature=settings.temperature,
        timeout_s=settings.request_timeout_s,
        max_output_tokens=settings.max_output_tokens,
    )
    mode = AssistantMode.CHAT if options.chat else AssistantMode(options.mode)
    conversation_id = uuid.UUID(options.conversation) if options.conversation else None

    try:
        async with AgentMcpClient(settings.mcp_server_url, allowed=ASSISTANT_READ_TOOLS) as mcp:
            calls = (
                DriverCallService(ConsoleChannel(NotificationChannel.CALL), guardrails=settings.guardrails())
                if settings.driver_call_enabled
                else None
            )
            service = AssistantService(
                llm,
                mcp,
                AssistantActions(database, mcp, driver_calls=calls),
                database=database,
            )

            questions = _questions(options, mode)
            for question in questions:
                answer = await service.respond(
                    AssistantRequest(
                        operator_id=options.operator,
                        message=question,
                        mode=mode,
                        conversation_id=conversation_id,
                        tms_ids=tuple(options.tms),
                        allow_actions=options.allow_actions,
                        mission_id=options.mission,
                        alert_id=uuid.UUID(options.alert) if options.alert else None,
                    )
                )
                conversation_id = answer.conversation_id
                _render(answer)
    finally:
        await llm.aclose()
        await database.dispose()
    return 0


def _questions(options: argparse.Namespace, mode: AssistantMode) -> Iterator[str]:
    """Rend les questions, une par une : argument, puis saisie si interactif."""
    message = " ".join(options.message).strip()
    if message:
        yield message
    if not options.chat:
        if not message and mode is AssistantMode.REPORT:
            yield "Fais le point sur la situation."
        return
    while True:
        try:
            saisie = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if saisie in ("", "quit", "exit"):
            return
        yield saisie


def _render(answer: AssistantAnswer) -> None:
    """Affiche la réponse, ses sources et ses actions.

    Les sources et les actions sont montrées séparément du texte : c'est ce qui
    permet de vérifier qu'une réponse s'appuie sur une lecture réelle, plutôt
    que sur ce que le modèle croit savoir.
    """
    print(f"\n{answer.answer}\n")
    if answer.sources:
        lectures = ", ".join(source.tool for source in answer.sources)
        print(f"  lectures : {lectures}")
    for action in answer.actions:
        marque = "✓" if action.succeeded else "✗"
        print(f"  {marque} {action.kind.value} — {action.detail}")
    if answer.truncated:
        print("  ⚠ réponse coupée par la limite de longueur du modèle")
    print(
        f"  conversation {answer.conversation_id} · {answer.rounds} tour(s) · "
        f"{answer.input_tokens} in / {answer.output_tokens} out"
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s %(message)s")
    raise SystemExit(asyncio.run(_run(_parse(sys.argv[1:]))))


if __name__ == "__main__":
    main()
