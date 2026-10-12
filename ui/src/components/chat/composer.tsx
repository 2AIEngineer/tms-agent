import { useQuery } from '@tanstack/react-query'
import { ArrowUp, Bell, CircleHelp, FileText, MessageSquare, Paperclip, Square, Truck, X, Zap } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useApp } from '@/app-context'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { missionIdOf } from '@/lib/format'
import type { AssistantMode } from '@/lib/types'
import { cn } from '@/lib/utils'
import { SeverityDot } from '../severity'

const MODES: { value: AssistantMode; label: string; hint: string; icon: typeof MessageSquare }[] = [
  { value: 'chat', label: 'Conversation', hint: 'Échanges suivis : l’assistant se souvient du fil.', icon: MessageSquare },
  { value: 'ask', label: 'Question', hint: 'Une question précise, une réponse factuelle et sourcée.', icon: CircleHelp },
  { value: 'report', label: 'Rapport', hint: 'Synthèse de la situation de la flotte.', icon: FileText },
]

function ContextPicker() {
  const { context, setContext } = useApp()
  const [open, setOpen] = useState(false)
  const alerts = useQuery({
    queryKey: ['alerts', 'picker'],
    queryFn: () => api.alerts({ status: 'open', limit: 30 }),
    enabled: open,
  })
  const missions = useQuery({ queryKey: ['missions'], queryFn: api.missions, enabled: open, retry: false })

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger
          render={
            <PopoverTrigger
              render={
                <Button variant="ghost" size="icon-sm" aria-label="Rattacher une alerte ou une mission">
                  <Paperclip />
                </Button>
              }
            />
          }
        />
        <TooltipContent>Rattacher une alerte ou une mission</TooltipContent>
      </Tooltip>
      <PopoverContent align="start" side="top" className="w-96 p-0">
        <Command>
          <CommandInput placeholder="Chercher une mission ou une alerte…" />
          <CommandList className="max-h-72">
            <CommandEmpty>Rien à rattacher.</CommandEmpty>
            {alerts.data && alerts.data.alerts.length > 0 && (
              <CommandGroup heading="Alertes ouvertes">
                {alerts.data.alerts.map((a) => (
                  <CommandItem
                    key={a.id}
                    value={`alerte ${a.title} ${a.mission_id} ${a.id}`}
                    onSelect={() => {
                      setContext({ ...context, alertId: a.id, alertTitle: a.title, missionId: a.mission_id, tmsId: a.tms_id })
                      setOpen(false)
                    }}
                  >
                    <SeverityDot severity={a.severity} />
                    <span className="truncate">{a.title}</span>
                    <span className="ml-auto font-mono text-xs text-muted-foreground">{a.mission_id}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {missions.data && missions.data.missions.length > 0 && (
              <CommandGroup heading="Missions actives">
                {missions.data.missions.map((m) => {
                  const id = missionIdOf(m)
                  return (
                    <CommandItem
                      key={`${m.tms_id}/${id}`}
                      value={`mission ${id} ${m.tms_id}`}
                      onSelect={() => {
                        setContext({ ...context, missionId: id, tmsId: m.tms_id })
                        setOpen(false)
                      }}
                    >
                      <Truck className="text-muted-foreground" />
                      <span className="font-mono text-xs">{id}</span>
                      <span className="ml-auto text-xs text-muted-foreground">{m.tms_id}</span>
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

export function Composer() {
  const { chat, me, context, setContext, draft, setDraft } = useApp()
  const [text, setText] = useState('')
  const [mode, setMode] = useState<AssistantMode>('chat')
  const [allowActions, setAllowActions] = useState(false)
  const area = useRef<HTMLTextAreaElement>(null)
  const canAct = me?.can_act ?? false

  // Brouillon venu d'ailleurs (« Demander à l'assistant » depuis une alerte).
  useEffect(() => {
    if (!draft) return
    setText(draft.text)
    if (draft.mode) setMode(draft.mode)
    setDraft(null)
    requestAnimationFrame(() => area.current?.focus())
  }, [draft, setDraft])

  useEffect(() => {
    const el = area.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`
  }, [text])

  useEffect(() => {
    if (!canAct) setAllowActions(false)
  }, [canAct])

  const submit = () => {
    const message = text.trim() || (mode === 'report' ? 'Fais-moi un rapport de situation.' : '')
    if (!message || chat.busy) return
    setText('')
    void chat.send({
      message,
      mode,
      allowActions: allowActions && canAct,
      missionId: context.missionId,
      alertId: context.alertId,
    })
  }

  const active = MODES.find((m) => m.value === mode)!
  const hasContext = context.missionId || context.alertId

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4 sm:px-6">
      <div
        className={cn(
          'rounded-2xl border bg-card shadow-sm transition-shadow focus-within:shadow-md focus-within:ring-2 focus-within:ring-ring/30',
          allowActions && 'border-warning/50 focus-within:ring-warning/30',
        )}
      >
        {hasContext && (
          <div className="flex flex-wrap gap-1.5 px-3 pt-3">
            {context.alertId && (
              <span className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-accent py-1 pl-2.5 pr-1.5 text-xs text-accent-foreground">
                <Bell className="size-3 shrink-0" />
                <span className="truncate">{context.alertTitle ?? `Alerte ${context.alertId.slice(0, 8)}`}</span>
                <button
                  aria-label="Retirer l'alerte"
                  onClick={() => setContext({ ...context, alertId: undefined, alertTitle: undefined })}
                  className="rounded-full p-0.5 hover:bg-foreground/10"
                >
                  <X className="size-3" />
                </button>
              </span>
            )}
            {context.missionId && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-accent py-1 pl-2.5 pr-1.5 text-xs text-accent-foreground">
                <Truck className="size-3" />
                <span className="font-mono">{context.missionId}</span>
                <button
                  aria-label="Retirer la mission"
                  onClick={() => setContext({ ...context, missionId: undefined, tmsId: undefined })}
                  className="rounded-full p-0.5 hover:bg-foreground/10"
                >
                  <X className="size-3" />
                </button>
              </span>
            )}
          </div>
        )}

        <textarea
          ref={area}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submit()
            }
          }}
          maxLength={4000}
          rows={1}
          placeholder={
            mode === 'report'
              ? 'Précisez le rapport voulu (facultatif)…'
              : allowActions
                ? 'Demandez une analyse ou une action : clore une alerte, appeler un chauffeur…'
                : 'Posez une question sur vos missions et vos alertes…'
          }
          className="block max-h-56 w-full resize-none bg-transparent px-4 py-3 text-[0.925rem] leading-relaxed outline-none placeholder:text-muted-foreground"
        />

        <div className="flex flex-wrap items-center gap-1.5 px-2.5 pb-2.5">
          <ContextPicker />

          <div className="flex rounded-lg bg-muted p-0.5" role="radiogroup" aria-label="Mode">
            {MODES.map((m) => (
              <Tooltip key={m.value}>
                <TooltipTrigger
                  render={
                    <button
                      role="radio"
                      aria-checked={mode === m.value}
                      onClick={() => setMode(m.value)}
                      className={cn(
                        'flex h-6 items-center gap-1 rounded-md px-2 text-xs font-medium text-muted-foreground transition-colors',
                        mode === m.value && 'bg-background text-foreground shadow-sm',
                      )}
                    >
                      <m.icon className="size-3.5" />
                      <span className="hidden sm:inline">{m.label}</span>
                    </button>
                  }
                />
                <TooltipContent>{m.hint}</TooltipContent>
              </Tooltip>
            ))}
          </div>

          <Tooltip>
            <TooltipTrigger
              render={
                <label
                  className={cn(
                    'flex h-7 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-xs font-medium transition-colors',
                    allowActions ? 'bg-warning/15 text-warning' : 'text-muted-foreground hover:bg-muted',
                    !canAct && 'cursor-not-allowed opacity-50',
                  )}
                >
                  <Zap className="size-3.5" />
                  Autoriser d’agir
                  <Switch size="sm" checked={allowActions} disabled={!canAct} onCheckedChange={setAllowActions} />
                </label>
              }
            />
            <TooltipContent className="max-w-64">
              {canAct
                ? 'L’assistant pourra préparer une action (clore une alerte, appeler un chauffeur, consigner une décision). Rien ne part sans votre validation.'
                : 'Votre rôle ne permet pas d’agir : vous pouvez seulement poser des questions.'}
            </TooltipContent>
          </Tooltip>

          <div className="ml-auto flex items-center gap-2">
            {text.length > 3500 && (
              <span className="text-xs tabular-nums text-muted-foreground">{text.length}/4000</span>
            )}
            {chat.busy ? (
              <Button size="icon" variant="secondary" onClick={chat.stop} aria-label="Arrêter">
                <Square className="fill-current" />
              </Button>
            ) : (
              <Button
                size="icon"
                onClick={submit}
                disabled={!text.trim() && mode !== 'report'}
                aria-label={`Envoyer (${active.label})`}
              >
                <ArrowUp />
              </Button>
            )}
          </div>
        </div>
      </div>
      <p className="mt-2 text-center text-xs text-muted-foreground">
        L’assistant lit le TMS en direct. Les règles détectent, l’assistant explique — vérifiez les sources avant d’agir.
      </p>
    </div>
  )
}
