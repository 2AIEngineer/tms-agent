import { useQuery } from '@tanstack/react-query'
import { ArrowDown, Loader2, Plus } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useApp } from '@/app-context'
import { AssistantTurn, OperatorTurn } from '@/components/chat/turn'
import { Composer } from '@/components/chat/composer'
import { Welcome } from '@/components/chat/welcome'
import { PageHeader } from '@/components/page'
import { Button } from '@/components/ui/button'
import { api } from '@/lib/api'

export function AssistantView() {
  const { chat, newConversation } = useApp()
  const scroller = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const last = chat.turns[chat.turns.length - 1]

  const title = useQuery({
    queryKey: ['conversations'],
    queryFn: api.conversations,
    select: (d) => d.conversations.find((c) => c.conversation_id === chat.conversationId)?.title,
    enabled: !!chat.conversationId,
  })

  const scrollToEnd = useCallback((smooth = false) => {
    const el = scroller.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  // Suit le flux tant que l'opérateur n'a pas remonté : lire un passage plus haut ne doit pas être interrompu.
  useEffect(() => {
    if (pinned) scrollToEnd()
  }, [chat.turns, pinned, scrollToEnd])

  const retry = () => {
    const lastOperator = [...chat.turns].reverse().find((t) => t.role === 'operator')
    if (!lastOperator) return
    void chat.send({
      message: lastOperator.text,
      mode: 'chat',
      allowActions: false,
      missionId: lastOperator.context?.missionId,
      alertId: lastOperator.context?.alertId,
    })
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={title.data ?? (chat.turns.length ? 'Conversation' : 'Assistant')}
        description={chat.conversationId ? undefined : 'Questions, rapports et actions validées par vous'}
        actions={
          chat.turns.length > 0 && (
            <Button variant="outline" size="sm" onClick={newConversation}>
              <Plus /> Nouvelle
            </Button>
          )
        }
      />

      <div className="relative min-h-0 flex-1">
        <div
          ref={scroller}
          onScroll={(e) => {
            const el = e.currentTarget
            setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 80)
          }}
          className="h-full overflow-y-auto"
        >
          {chat.loading ? (
            <div className="flex h-full items-center justify-center text-muted-foreground">
              <Loader2 className="size-5 animate-spin" />
            </div>
          ) : chat.turns.length === 0 ? (
            <div className="flex h-full">
              <Welcome />
            </div>
          ) : (
            <div className="mx-auto max-w-3xl space-y-7 px-4 py-6 sm:px-6">
              {chat.turns.map((turn) =>
                turn.role === 'operator' ? (
                  <OperatorTurn key={turn.id} turn={turn} />
                ) : (
                  <AssistantTurn
                    key={turn.id}
                    turn={turn}
                    actions={chat.actions}
                    isLast={turn === last}
                    onRetry={retry}
                  />
                ),
              )}
            </div>
          )}
        </div>
        {!pinned && chat.turns.length > 0 && (
          <Button
            size="icon-sm"
            variant="outline"
            className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full shadow-md"
            onClick={() => {
              setPinned(true)
              scrollToEnd(true)
            }}
            aria-label="Aller au dernier message"
          >
            <ArrowDown />
          </Button>
        )}
      </div>

      <Composer />
    </div>
  )
}
