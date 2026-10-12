import { Check, ChevronRight, CircleAlert, Eye, Loader2, Sparkles, Zap } from 'lucide-react'
import { useState } from 'react'
import type { ToolStep } from '@/hooks/use-chat'
import { toolLabel } from '@/lib/format'
import { cn } from '@/lib/utils'

function StepRow({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false)
  const args = Object.entries(step.args)
  const expandable = !!step.preview || args.length > 0
  const Icon = step.role === 'action' ? Zap : Eye

  return (
    <li className="relative pl-6">
      <span
        className={cn(
          'absolute left-0 top-0.5 flex size-4 items-center justify-center rounded-full',
          step.state === 'running' && 'text-primary',
          step.state === 'ok' && 'bg-success/15 text-success',
          step.state === 'error' && 'bg-destructive/15 text-destructive',
        )}
      >
        {step.state === 'running' ? (
          <Loader2 className="size-3.5 animate-spin" />
        ) : step.state === 'ok' ? (
          <Check className="size-3" />
        ) : (
          <CircleAlert className="size-3" />
        )}
      </span>
      <button
        type="button"
        disabled={!expandable}
        onClick={() => setOpen((o) => !o)}
        className="group flex w-full items-center gap-2 text-left text-[13px] disabled:cursor-default"
      >
        <Icon className={cn('size-3.5 shrink-0', step.role === 'action' ? 'text-warning' : 'text-muted-foreground')} />
        <span className="font-medium">{toolLabel(step.tool)}</span>
        {args.length > 0 && !open && (
          <span className="truncate font-mono text-xs text-muted-foreground">
            {args.map(([k, v]) => `${k}=${v}`).join(' · ')}
          </span>
        )}
        {step.durationMs !== undefined && (
          <span className="ml-auto shrink-0 text-xs tabular-nums text-muted-foreground">
            {step.durationMs < 1000 ? `${step.durationMs} ms` : `${(step.durationMs / 1000).toFixed(1)} s`}
          </span>
        )}
        {expandable && (
          <ChevronRight
            className={cn('size-3.5 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')}
          />
        )}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          {args.length > 0 && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 rounded-lg bg-muted/60 px-3 py-2 font-mono text-xs">
              {args.map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k}</dt>
                  <dd className="break-all">{v}</dd>
                </div>
              ))}
            </dl>
          )}
          {step.preview && (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/60 px-3 py-2 font-mono text-xs text-muted-foreground">
              {step.preview}
            </pre>
          )}
        </div>
      )}
    </li>
  )
}

/** Le « raisonnement visible » : quels outils, dans quel ordre, avec quel résultat. */
export function StepTimeline({
  steps,
  reasoning,
  round,
  streaming,
}: {
  steps: ToolStep[]
  reasoning: string
  round?: { round: number; max: number }
  streaming: boolean
}) {
  const [open, setOpen] = useState(true)
  const [showReasoning, setShowReasoning] = useState(false)
  if (!steps.length && !reasoning && !(streaming && round)) return null

  const running = steps.some((s) => s.state === 'running')
  const label = streaming
    ? running
      ? 'Consultation en cours…'
      : 'Réflexion…'
    : `${steps.length} consultation${steps.length > 1 ? 's' : ''}`

  return (
    <div className="mb-3 rounded-xl border bg-card/60">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        {streaming ? <Loader2 className="size-3.5 animate-spin text-primary" /> : <Sparkles className="size-3.5" />}
        <span>{label}</span>
        {streaming && round && (
          <span className="rounded-full bg-muted px-1.5 py-0.5 tabular-nums">
            tour {round.round}/{round.max}
          </span>
        )}
        <ChevronRight className={cn('ml-auto size-3.5 transition-transform', open && 'rotate-90')} />
      </button>
      {open && (steps.length > 0 || reasoning) && (
        <div className="space-y-2.5 border-t px-3 py-2.5">
          {reasoning && (
            <div>
              <button
                type="button"
                className="text-xs font-medium text-muted-foreground hover:text-foreground"
                onClick={() => setShowReasoning((s) => !s)}
              >
                {showReasoning ? 'Masquer' : 'Voir'} le raisonnement du modèle
              </button>
              {showReasoning && (
                <p className="mt-1.5 whitespace-pre-wrap rounded-lg bg-muted/60 px-3 py-2 text-xs italic leading-relaxed text-muted-foreground">
                  {reasoning}
                </p>
              )}
            </div>
          )}
          {steps.length > 0 && (
            <ol className="space-y-2 before:absolute">
              {steps.map((s) => (
                <StepRow key={s.callId} step={s} />
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  )
}
