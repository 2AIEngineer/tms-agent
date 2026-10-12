import { Ban, Check, CircleAlert, Clock, Loader2, PhoneCall, ShieldCheck, ClipboardList, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { useApp } from '@/app-context'
import type { ActionView } from '@/hooks/use-chat'
import { ACTION_LABEL } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { ActionKind, PendingActionStatus } from '@/lib/types'

const ICON: Record<ActionKind, typeof Check> = {
  close_alert: ShieldCheck,
  call_driver: PhoneCall,
  log_decision: ClipboardList,
}

const FINAL: Record<Exclude<PendingActionStatus, 'proposed'>, { label: string; tone: string; icon: typeof Check }> = {
  executing: { label: 'Exécution en cours', tone: 'text-primary', icon: Loader2 },
  executed: { label: 'Exécutée', tone: 'text-success', icon: Check },
  failed: { label: 'Échec', tone: 'text-destructive', icon: CircleAlert },
  rejected: { label: 'Refusée', tone: 'text-muted-foreground', icon: Ban },
  expired: { label: 'Expirée', tone: 'text-muted-foreground', icon: Clock },
}

function useRemaining(expiresAt: string | null) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!expiresAt) return
    const timer = setInterval(() => setNow(Date.now()), 15_000)
    return () => clearInterval(timer)
  }, [expiresAt])
  return expiresAt ? new Date(expiresAt).getTime() - now : null
}

function formatRemaining(ms: number) {
  const minutes = Math.ceil(ms / 60_000)
  if (minutes >= 60) return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')}`
  return `${minutes} min`
}

/**
 * L'opérateur valide **cette ligne** : le résumé est construit par le serveur à
 * partir des arguments enregistrés, la prose du modèle ne fait pas foi.
 */
export function ActionCard({ action }: { action: ActionView }) {
  const { chat, me } = useApp()
  const Icon = ICON[action.kind]
  const remaining = useRemaining(action.status === 'proposed' ? action.expires_at : null)
  const expired = action.status === 'expired' || (remaining !== null && remaining <= 0)
  const proposed = action.status === 'proposed' && !expired
  const canAct = me?.can_act ?? true
  const final = proposed ? null : FINAL[expired && action.status === 'proposed' ? 'expired' : (action.status as keyof typeof FINAL)]
  const args = Object.entries(action.arguments)

  return (
    <div
      className={cn(
        'overflow-hidden rounded-2xl border bg-card shadow-sm',
        proposed && 'border-warning/40 ring-1 ring-warning/15',
      )}
    >
      <div className="flex items-center gap-2.5 border-b bg-muted/40 px-4 py-2.5">
        <span
          className={cn(
            'flex size-7 items-center justify-center rounded-lg',
            proposed ? 'bg-warning/15 text-warning' : 'bg-muted text-muted-foreground',
          )}
        >
          <Icon className="size-4" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium leading-tight">{ACTION_LABEL[action.kind]}</p>
          <p className="text-xs text-muted-foreground">
            {proposed ? 'Action en attente de votre validation' : 'Action proposée par l’assistant'}
          </p>
        </div>
        {final && (
          <span className={cn('flex items-center gap-1 text-xs font-medium', final.tone)}>
            <final.icon className={cn('size-3.5', action.status === 'executing' && 'animate-spin')} />
            {final.label}
          </span>
        )}
        {proposed && remaining !== null && (
          <span className="flex items-center gap-1 text-xs tabular-nums text-muted-foreground">
            <Clock className="size-3.5" />
            {formatRemaining(remaining)}
          </span>
        )}
      </div>

      <div className="space-y-3 px-4 py-3">
        <p className="text-sm font-medium leading-snug">{action.summary}</p>
        {action.reason && (
          <p className="text-sm text-muted-foreground">
            <span className="font-medium text-foreground/70">Motif · </span>
            {action.reason}
          </p>
        )}
        {(args.length > 0 || action.mission_id || action.tms_id) && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-lg bg-muted/50 px-3 py-2 text-xs">
            {action.tms_id && (
              <>
                <dt className="text-muted-foreground">TMS</dt>
                <dd className="font-mono">{action.tms_id}</dd>
              </>
            )}
            {action.mission_id && (
              <>
                <dt className="text-muted-foreground">Mission</dt>
                <dd className="font-mono">{action.mission_id}</dd>
              </>
            )}
            {args.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted-foreground">{k}</dt>
                <dd className="break-words font-mono">{v}</dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {proposed && (
        <div className="flex items-center justify-end gap-2 border-t bg-muted/30 px-4 py-2.5">
          {!canAct && <span className="mr-auto text-xs text-muted-foreground">Votre rôle ne permet pas d’agir.</span>}
          <Button
            variant="ghost"
            size="sm"
            disabled={!!action.busy}
            onClick={() => void chat.decide(action.id, 'reject')}
          >
            {action.busy === 'reject' ? <Loader2 className="animate-spin" /> : <X />}
            Refuser
          </Button>
          <Button size="sm" disabled={!!action.busy || !canAct} onClick={() => void chat.decide(action.id, 'confirm')}>
            {action.busy === 'confirm' ? <Loader2 className="animate-spin" /> : <Check />}
            Valider et exécuter
          </Button>
        </div>
      )}
    </div>
  )
}
