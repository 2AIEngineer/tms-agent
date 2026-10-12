import { useQuery } from '@tanstack/react-query'
import { Bell, CheckCircle2, MessageSquareText, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { useApp } from '@/app-context'
import { EmptyState, ErrorNotice, PageHeader } from '@/components/page'
import { SeverityBadge, SeverityDot } from '@/components/severity'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { api } from '@/lib/api'
import { CATEGORY_LABEL, SEVERITY_LABEL, SEVERITY_ORDER, dateTime, relativeTime } from '@/lib/format'
import type { AlertDetail, AlertRow, AlertStatus, Severity } from '@/lib/types'
import { cn } from '@/lib/utils'

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'inline-flex h-7 items-center gap-1.5 rounded-full border px-3 text-xs font-medium transition-colors',
        active ? 'border-primary/40 bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-muted',
      )}
    >
      {children}
    </button>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

function DetailSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { askAbout } = useApp()
  const detail = useQuery({ queryKey: ['alert', id], queryFn: () => api.alert(id!), enabled: !!id })
  const a: AlertDetail | undefined = detail.data
  const result = a?.investigation?.result
  const analysis = typeof result === 'string' ? result : result ? JSON.stringify(result, null, 2) : null

  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{a?.title ?? 'Alerte'}</SheetTitle>
          <SheetDescription>
            {a ? `${a.tms_id} · mission ${a.mission_id} · ${CATEGORY_LABEL[a.category] ?? a.category}` : 'Chargement…'}
          </SheetDescription>
        </SheetHeader>
        {detail.isLoading && <Skeleton className="mx-4 h-40" />}
        {detail.error && <ErrorNotice error={detail.error} />}
        {a && (
          <div className="space-y-6 px-4 pb-6">
            <div className="flex flex-wrap items-center gap-2">
              <SeverityBadge severity={a.severity} />
              <Badge variant={a.status === 'open' ? 'destructive' : 'secondary'}>
                {a.status === 'open' ? 'Ouverte' : 'Résolue'}
              </Badge>
              <span className="text-xs text-muted-foreground">Détectée {dateTime(a.detected_at)}</span>
            </div>
            <p className="text-sm leading-relaxed">{a.message}</p>
            <Button
              onClick={() => {
                askAbout(
                  { alertId: a.id, alertTitle: a.title, missionId: a.mission_id, tmsId: a.tms_id },
                  'Explique cette alerte : que s’est-il passé, et que me conseilles-tu ?',
                )
                onClose()
              }}
            >
              <MessageSquareText /> Demander à l’assistant
            </Button>

            {analysis && (
              <Section title="Analyse de l’enquête">
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-xl bg-muted/60 p-3 font-sans text-sm leading-relaxed">
                  {analysis}
                </pre>
                {a.investigation?.model && (
                  <p className="mt-1.5 text-xs text-muted-foreground">Modèle : {a.investigation.model}</p>
                )}
              </Section>
            )}
            {a.investigation?.error && (
              <Section title="Enquête">
                <p className="text-sm text-destructive">{a.investigation.error}</p>
              </Section>
            )}

            {a.notifications.length > 0 && (
              <Section title="Notifications">
                <ul className="divide-y rounded-xl border">
                  {a.notifications.map((n, i) => (
                    <li key={i} className="flex items-center gap-3 px-3 py-2 text-sm">
                      <span className="font-medium uppercase text-muted-foreground text-xs w-10">{n.channel}</span>
                      <span className="min-w-0 flex-1 truncate">
                        {n.recipient_name ?? n.recipient_role ?? '—'}
                        {n.management_level && <span className="text-muted-foreground"> · {n.management_level}</span>}
                      </span>
                      <Badge variant={n.status === 'failed' ? 'destructive' : 'secondary'}>{n.status}</Badge>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {a.driver_calls.length > 0 && (
              <Section title="Appels au chauffeur">
                <ul className="divide-y rounded-xl border">
                  {a.driver_calls.map((c, i) => (
                    <li key={i} className="px-3 py-2 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <span>{c.reason}</span>
                        <Badge variant="secondary">{c.status}</Badge>
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {c.requested_by} · {dateTime(c.requested_at)}
                        {c.blocked_reason && ` · bloqué : ${c.blocked_reason}`}
                      </p>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {a.audit.length > 0 && (
              <Section title="Journal d’audit">
                <ol className="space-y-2 border-l pl-4">
                  {a.audit.map((e, i) => (
                    <li key={i} className="relative text-sm">
                      <span className="absolute -left-[1.3rem] top-1.5 size-2 rounded-full bg-border" />
                      <span className="font-medium">{e.action}</span>
                      <span className="text-muted-foreground"> · {e.actor}</span>
                      <p className="text-xs text-muted-foreground">{dateTime(e.occurred_at)}</p>
                    </li>
                  ))}
                </ol>
              </Section>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}

function AlertItem({ alert, onOpen }: { alert: AlertRow; onOpen: () => void }) {
  const { askAbout } = useApp()
  return (
    <li className="group relative">
      <button onClick={onOpen} className="flex w-full items-start gap-3 px-4 py-3.5 text-left hover:bg-muted/50 sm:px-6">
        <SeverityDot severity={alert.severity} className="mt-1.5 size-2.5" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <p className="font-medium leading-snug">{alert.title}</p>
            {alert.status === 'resolved' && (
              <span className="flex items-center gap-1 text-xs text-success">
                <CheckCircle2 className="size-3.5" /> Résolue
              </span>
            )}
          </div>
          <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{alert.message}</p>
          <p className="mt-1.5 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            <span>{SEVERITY_LABEL[alert.severity]}</span>·<span>{CATEGORY_LABEL[alert.category] ?? alert.category}</span>·
            <span className="font-mono">{alert.mission_id}</span>·<span>{alert.tms_id}</span>·
            <span>{relativeTime(alert.detected_at)}</span>
          </p>
        </div>
      </button>
      <Button
        variant="outline"
        size="sm"
        className="absolute right-4 top-3 hidden bg-background group-hover:inline-flex sm:right-6"
        onClick={() =>
          askAbout(
            { alertId: alert.id, alertTitle: alert.title, missionId: alert.mission_id, tmsId: alert.tms_id },
            'Explique cette alerte : que s’est-il passé, et que me conseilles-tu ?',
          )
        }
      >
        <MessageSquareText /> Demander
      </Button>
    </li>
  )
}

export function AlertsView() {
  const [status, setStatus] = useState<AlertStatus | 'all'>('open')
  const [severity, setSeverity] = useState<Severity | 'all'>('all')
  const [openId, setOpenId] = useState<string | null>(null)

  const summary = useQuery({ queryKey: ['alerts', 'summary'], queryFn: api.alertSummary, refetchInterval: 20_000 })
  const list = useQuery({
    queryKey: ['alerts', 'list', status, severity],
    queryFn: () =>
      api.alerts({
        status: status === 'all' ? undefined : status,
        min_severity: severity === 'all' ? undefined : severity,
        limit: 100,
      }),
    refetchInterval: 20_000,
  })

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Alertes"
        description="Détectées par les règles métier ; l’assistant explique, il ne décide pas."
        actions={
          <Button variant="outline" size="sm" onClick={() => void list.refetch()}>
            <RefreshCw className={cn(list.isFetching && 'animate-spin')} /> Actualiser
          </Button>
        }
      />
      <div className="grid grid-cols-2 gap-3 border-b px-4 py-4 sm:grid-cols-5 sm:px-6">
        <div className="rounded-xl border bg-card px-3.5 py-3">
          <p className="text-xs text-muted-foreground">Ouvertes</p>
          <p className="mt-0.5 text-2xl font-semibold tabular-nums">{summary.data?.open_alerts ?? '–'}</p>
        </div>
        {SEVERITY_ORDER.map((s) => (
          <div key={s} className="rounded-xl border bg-card px-3.5 py-3">
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <SeverityDot severity={s} /> {SEVERITY_LABEL[s]}
            </p>
            <p className="mt-0.5 text-2xl font-semibold tabular-nums">{summary.data?.by_severity[s] ?? 0}</p>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5 sm:px-6">
        {(['open', 'resolved', 'all'] as const).map((s) => (
          <Chip key={s} active={status === s} onClick={() => setStatus(s)}>
            {{ open: 'Ouvertes', resolved: 'Résolues', all: 'Toutes' }[s]}
          </Chip>
        ))}
        <span className="mx-1 h-4 w-px bg-border" />
        <Chip active={severity === 'all'} onClick={() => setSeverity('all')}>
          Toutes sévérités
        </Chip>
        {SEVERITY_ORDER.map((s) => (
          <Chip key={s} active={severity === s} onClick={() => setSeverity(s)}>
            ≥ {SEVERITY_LABEL[s]}
          </Chip>
        ))}
        {list.data && (
          <span className="ml-auto text-xs tabular-nums text-muted-foreground">
            {list.data.count} / {list.data.total}
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {list.isLoading && (
          <div className="space-y-px">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-20 rounded-none" />
            ))}
          </div>
        )}
        {list.error && <ErrorNotice error={list.error} onRetry={() => void list.refetch()} />}
        {list.data && list.data.alerts.length === 0 && (
          <EmptyState icon={<Bell />} title="Aucune alerte" description="Rien ne correspond à ces filtres." />
        )}
        {list.data && list.data.alerts.length > 0 && (
          <ul className="divide-y">
            {list.data.alerts.map((a) => (
              <AlertItem key={a.id} alert={a} onOpen={() => setOpenId(a.id)} />
            ))}
          </ul>
        )}
      </div>
      <DetailSheet id={openId} onClose={() => setOpenId(null)} />
    </div>
  )
}
