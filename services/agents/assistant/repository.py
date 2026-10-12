"""Historique des conversations (F25).

Une conversation survit au redémarrage du service, et c'est tout ce que F25
demande. L'implémentation est volontairement ordinaire : deux tables, des
insertions en ajout seul, une relecture bornée.

## Pourquoi pas le *checkpointer* LangGraph

La spécification mentionne LangGraph et son *checkpointer* PostgreSQL, et nous
ne les employons pas. Trois raisons, dans l'ordre d'importance :

  - **cet historique est lu par des humains.** L'API d'exploitation doit
    pouvoir afficher une conversation à l'opérateur, et un auditeur relire ce
    que l'assistant a répondu avant une décision. Un état de graphe sérialisé
    ne se montre pas et ne s'interroge pas en SQL ;
  - **un seul pilote de base.** Le *checkpointer* officiel s'appuie sur
    psycopg ; tout le reste du système parle asyncpg via SQLAlchemy Core.
    Ajouter un second pilote et un second pool pour conserver des messages
    serait un coût permanent pour un gain nul ;
  - **il n'y a pas de graphe à reprendre.** L'assistant répond en un
    aller-retour borné. Un *checkpointer* sert à reprendre une exécution
    interrompue au milieu ; ici, une exécution interrompue est simplement
    rejouée par l'opérateur, qui repose sa question.

Ce qui est conservé, en revanche, est plus que des tours de parole : les
lectures MCP y figurent (`role = 'tool'`), de sorte qu'une réponse reste
vérifiable après coup.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from database.schema import conversation_messages, conversations
from shared.mapped_dto import AssistantMode, ConversationMessage, MessageRole

# Longueur du titre dérivé de la première question. Au-delà, une liste de
# conversations devient illisible.
TITLE_MAX_LENGTH = 200

# Messages relus pour construire le contexte. Un plafond est indispensable :
# l'historique entier est renvoyé au modèle à chaque tour, et une conversation
# d'une heure dépasserait la fenêtre de contexte à elle seule.
DEFAULT_HISTORY_LIMIT = 20


class ConversationRepository:
    """Lecture et écriture de l'historique des conversations."""

    async def open(
        self,
        connection: AsyncConnection,
        *,
        operator_id: str,
        title: str,
        mode: AssistantMode,
    ) -> uuid.UUID:
        conversation_id = uuid.uuid4()
        await connection.execute(
            insert(conversations).values(
                id=conversation_id,
                operator_id=operator_id,
                title=title[:TITLE_MAX_LENGTH],
                mode=mode.value,
            )
        )
        return conversation_id

    async def belongs_to(
        self,
        connection: AsyncConnection,
        conversation_id: uuid.UUID,
        operator_id: str,
    ) -> bool:
        """Vérifie qu'une conversation appartient bien à cet opérateur.

        Sans ce contrôle, un identifiant de conversation deviné donnerait accès
        à l'historique d'un collègue — et l'historique d'un superviseur peut
        porter sur des décisions qui ne concernent pas tout le monde.
        """
        result = await connection.execute(
            select(conversations.c.id).where(
                conversations.c.id == conversation_id,
                conversations.c.operator_id == operator_id,
            )
        )
        return result.first() is not None

    async def append(
        self,
        connection: AsyncConnection,
        conversation_id: uuid.UUID,
        messages: Sequence[ConversationMessage],
    ) -> int:
        """Ajoute des messages à la suite de la conversation.

        Le rang vient d'un `select max(seq)` **dans la même transaction** que
        l'insertion : deux fenêtres du même opérateur peuvent écrire en
        parallèle, et l'index unique sur `(conversation_id, seq)` fait alors
        échouer la seconde transaction au lieu de produire deux messages de
        même rang, indiscernables à la relecture.
        """
        if not messages:
            return 0
        next_seq = await self._next_seq(connection, conversation_id)
        await connection.execute(
            insert(conversation_messages),
            [
                {
                    "conversation_id": conversation_id,
                    "seq": next_seq + offset,
                    "role": message.role.value,
                    "content": message.content,
                    "tool_name": message.tool_name,
                }
                for offset, message in enumerate(messages)
            ],
        )
        # `updated_at` porte l'ordre d'affichage de la liste des conversations.
        await connection.execute(
            update(conversations).where(conversations.c.id == conversation_id).values(updated_at=func.now())
        )
        return len(messages)

    async def history(
        self,
        connection: AsyncConnection,
        conversation_id: uuid.UUID,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[ConversationMessage]:
        """Derniers messages de la conversation, dans l'ordre chronologique.

        Les plus **récents** sont retenus, puis remis dans l'ordre : tronquer
        par le début donnerait au modèle le préambule d'une conversation et lui
        cacherait ce dont il est question maintenant.
        """
        result = await connection.execute(
            select(
                conversation_messages.c.seq,
                conversation_messages.c.role,
                conversation_messages.c.content,
                conversation_messages.c.tool_name,
                conversation_messages.c.created_at,
            )
            .where(conversation_messages.c.conversation_id == conversation_id)
            .order_by(conversation_messages.c.seq.desc())
            .limit(limit)
        )
        rows = list(result.mappings())
        return [
            ConversationMessage(
                seq=row["seq"],
                role=MessageRole(row["role"]),
                content=row["content"],
                tool_name=row["tool_name"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]

    async def recent(
        self,
        connection: AsyncConnection,
        operator_id: str,
        *,
        mode: AssistantMode | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Conversations d'un opérateur, la plus récemment active d'abord."""
        query = select(
            conversations.c.id,
            conversations.c.title,
            conversations.c.mode,
            conversations.c.updated_at,
        ).where(conversations.c.operator_id == operator_id)
        if mode is not None:
            query = query.where(conversations.c.mode == mode.value)
        result = await connection.execute(query.order_by(conversations.c.updated_at.desc()).limit(limit))
        return [dict(row) for row in result.mappings()]

    async def last_answer(
        self,
        connection: AsyncConnection,
        conversation_id: uuid.UUID,
    ) -> str | None:
        """Dernière réponse de l'assistant dans cette conversation.

        Ce qu'une liste de rapports affiche : le texte, et non la suite des
        lectures qui l'ont produit.
        """
        result = await connection.execute(
            select(conversation_messages.c.content)
            .where(
                conversation_messages.c.conversation_id == conversation_id,
                conversation_messages.c.role == MessageRole.ASSISTANT.value,
            )
            .order_by(conversation_messages.c.seq.desc())
            .limit(1)
        )
        row = result.first()
        return str(row[0]) if row else None

    @staticmethod
    async def _next_seq(connection: AsyncConnection, conversation_id: uuid.UUID) -> int:
        result = await connection.execute(
            select(func.coalesce(func.max(conversation_messages.c.seq), 0)).where(
                conversation_messages.c.conversation_id == conversation_id
            )
        )
        return int(result.scalar_one()) + 1
