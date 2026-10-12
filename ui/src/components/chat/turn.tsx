import { AlertTriangle, Bot, Check, Copy, Cpu, Database, FileText, OctagonX, RotateCw, TriangleAlert, X } from 'lucide-react'
import { memo, useState } from 'react'
import { toast } from 'sonner'
import { Markdown } from '@/components/markdown'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { ActionView, Turn } from '@/hooks/use-chat'
import { ACTION_LABEL, formatTokens, toolLabel } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ActionCard } from './action-card'
import { StepTimeline } from './steps'

export function OperatorTurn({ turn }: { turn: Turn }) {
  const ctx = turn.context
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] space-y-1.5 sm:max-w-[75%]">
        {(ctx?.missionId || ctx?.alertId) && (
          <div className="flex flex-wrap justify-end gap-1.5">
            {ctx.missionId && (
              <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                mission {ctx.missionId}
              </span>
            )}
            {ctx.alertId && (
              <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
                alerte {ctx.alertId.slice(0, 8)}
              </span>
            )}
          </div>
        )}
        <div className="whitespace-pre-wrap rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-[0.925rem] leading-relaxed text-primary-foreground">
          {turn.text}
        </div>
      </div>
    </div>
  )
}

function AnswerMeta({ turn }: { turn: Turn }) {
  const answer = turn.answer
  const [open, setOpen] = useState(false)
  if (!answer) return null
  const sources = answer.sources
  return (
    <div className="mt-3 space-y-2">
      {answer.actions.length > 0 && (
        <ul className="space-y-1.5">
          {answer.actions.map((a, i) => (
            <li
              key={i}
              className={cn(
                'flex items-start gap-2 rounded-xl border px-3 py-2 text-sm',
                a.succeeded ? 'border-success/30 bg-success/8' : 'border-destructive/30 bg-destructive/5',
              )}
            >
              {a.succeeded ? (
                <Check className="mt-0.5 size-4 shrink-0 text-success" />
              ) : (
                <X className="mt-0.5 size-4 shrink-0 text-destructive" />
              )}
              <span>
                <span className="font-medium">{ACTION_LABEL[a.kind]}</span>
                {a.detail && <span className="text-muted-foreground"> — {a.detail}</span>}
              </span>
            </li>
          ))}
        </ul>
      )}

      {(sources.length > 0 || answer.input_tokens > 0) && (
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger className="flex items-center gap-3 text-xs text-muted-foreground hover:text-foreground">
            {sources.length > 0 && (
              <span className="flex items-center gap-1">
                <Database className="size-3.5" />
                {sources.length} source{sources.length > 1 ? 's' : ''}
              </span>
            )}
            <span className="flex items-center gap-1 tabular-nums">
              <Cpu className="size-3.5" />
              {formatTokens(answer.input_tokens)} → {formatTokens(answer.output_tokens)} jetons · {answer.rounds} tour
              {answer.rounds > 1 ? 's' : ''}
            </span>
          </CollapsibleTrigger>
          <CollapsibleContent>
            {sources.length > 0 && (
              <ul className="mt-2 space-y-1 rounded-xl border bg-card/60 p-2.5">
                {sources.map((s, i) => (
                  <li key={i} className="flex gap-2 text-xs">
                    <span className="shrink-0 font-medium">{toolLabel(s.tool)}</span>
                    {s.summary && <span className="truncate text-muted-foreground">{s.summary}</span>}
                  </li>
                ))}
              </ul>
            )}
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}

export const AssistantTurn = memo(function AssistantTurn({
  turn,
  actions,
  isLast,
  onRetry,
}: {
  turn: Turn
  actions: Record<string, ActionView>
  isLast: boolean
  onRetry?: () => void
}) {
  const answer = turn.answer
  const [copied, setCopied] = useState(false)
  const empty = !turn.text && !turn.steps.length && !turn.error && !turn.reasoning
  const cards = turn.actionIds.map((id) => actions[id]).filter(Boolean)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(turn.text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('Copie impossible.')
    }
  }

  return (
    <div className="group/turn flex gap-3">
      <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-primary/12 text-primary">
        <Bot className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <StepTimeline steps={turn.steps} reasoning={turn.reasoning} round={turn.round} streaming={turn.streaming} />

        {empty && turn.streaming && (
          <div className="flex items-center gap-1.5 py-2" aria-label="L'assistant démarre">
            {[0, 1, 2].map((i) => (
              <span
                key={i}
                className="size-1.5 animate-bounce rounded-full bg-muted-foreground/50"
                style={{ animationDelay: `${i * 120}ms` }}
              />
            ))}
          </div>
        )}

        {turn.text && <Markdown streaming={turn.streaming}>{turn.text}</Markdown>}

        {answer?.unavailable && (
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-warning/40 bg-warning/10 px-3 py-2.5 text-sm">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" />
            <span>
              L’assistant est indisponible (modèle injoignable). Ce n’est pas une absence de résultat : les alertes
              continuent d’être détectées sans lui.
            </span>
          </div>
        )}
        {answer?.truncated && (
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-border bg-muted/50 px-3 py-2.5 text-sm">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <span>
              Réponse coupée par la limite de longueur du modèle. Demandez-lui de poursuivre ou de resserrer sa
              réponse.
            </span>
          </div>
        )}
        {turn.error && (
          <div className="mt-2 flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/5 px-3 py-2.5 text-sm">
            <OctagonX className="mt-0.5 size-4 shrink-0 text-destructive" />
            <div className="min-w-0 flex-1">
              <p className="font-medium text-destructive">La réponse n’a pas pu aboutir</p>
              <p className="mt-0.5 text-muted-foreground">{turn.error}</p>
            </div>
            {isLast && onRetry && (
              <Button variant="outline" size="sm" onClick={onRetry}>
                <RotateCw /> Réessayer
              </Button>
            )}
          </div>
        )}

        {cards.length > 0 && (
          <div className="mt-3 space-y-3">
            {cards.map((a) => (
              <ActionCard key={a.id} action={a} />
            ))}
          </div>
        )}

        <AnswerMeta turn={turn} />

        {!turn.streaming && turn.text && (
          <div className="mt-1.5 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover/turn:opacity-100">
            <Button variant="ghost" size="xs" onClick={() => void copy()}>
              {copied ? <Check /> : <Copy />}
              {copied ? 'Copié' : 'Copier'}
            </Button>
          </div>
        )}
      </div>
    </div>
  )
})

export function ReportBadge() {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-[11px] font-medium text-accent-foreground">
      <FileText className="size-3" /> Rapport
    </span>
  )
}
