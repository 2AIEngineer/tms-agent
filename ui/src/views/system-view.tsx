import { useQuery } from '@tanstack/react-query'
import { Activity, CheckCircle2, CircleAlert, RefreshCw } from 'lucide-react'
import { ErrorNotice, PageHeader } from '@/components/page'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useApp } from '@/app-context'

function Tile({ label, ok, detail }: { label: string; ok: boolean | undefined; detail?: string }) {
  return (
    <div className="flex items-start gap-3 rounded-2xl border bg-card p-4">
      {ok === undefined ? (
        <Skeleton className="size-5 rounded-full" />
      ) : ok ? (
        <CheckCircle2 className="mt-0.5 size-5 text-success" />
      ) : (
        <CircleAlert className="mt-0.5 size-5 text-destructive" />
      )}
      <div className="min-w-0">
        <p className="font-medium">{label}</p>
        {detail && <p className="mt-0.5 break-words text-sm text-muted-foreground">{detail}</p>}
      </div>
    </div>
  )
}

export function SystemView() {
  const { me } = useApp()
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 15_000, retry: false })
  const monitoring = useQuery({ queryKey: ['monitoring'], queryFn: api.monitoring, refetchInterval: 15_000, retry: false })

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Système"
        description="Un worker arrêté ne produit aucune alerte, ce qui ressemble à une flotte sans anomalie : vérifiez-le ici."
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void health.refetch()
              void monitoring.refetch()
            }}
          >
            <RefreshCw className={cn((health.isFetching || monitoring.isFetching) && 'animate-spin')} /> Actualiser
          </Button>
        }
      />
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4 sm:p-6">
        <div className="grid gap-3 sm:grid-cols-2">
          <Tile
            label="API d’exploitation & base"
            ok={health.isLoading ? undefined : !!health.data?.ok}
            detail={health.error ? (health.error as Error).message : health.data?.database}
          />
          <Tile
            label="Authentification"
            ok={monitoring.data ? !monitoring.data.auth_disabled : undefined}
            detail={
              monitoring.data?.auth_disabled
                ? 'Désactivée (mode développement) : toutes les requêtes passent sous une identité fictive.'
                : 'Jeton Entra ID vérifié.'
            }
          />
        </div>

        {monitoring.error && <ErrorNotice error={monitoring.error} />}

        <section>
          <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold">
            <Activity className="size-4" /> Cycles de surveillance
          </h2>
          {monitoring.data && monitoring.data.cycles.length === 0 && (
            <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">
              Aucun cycle enregistré. Le worker de surveillance est-il lancé ?
            </p>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {monitoring.data?.cycles.map((c, i) => {
              const tms = String(c.tms_id ?? i)
              const at = String(c.last_cycle_at ?? c.updated_at ?? c.completed_at ?? '')
              return (
                <div key={tms} className="rounded-2xl border bg-card p-4">
                  <div className="flex items-center justify-between">
                    <p className="font-medium">{tms}</p>
                    {at && <span className="text-xs text-muted-foreground">{relativeTime(at)}</span>}
                  </div>
                  <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
                    {Object.entries(c)
                      .filter(([k, v]) => k !== 'tms_id' && v !== null && typeof v !== 'object')
                      .map(([k, v]) => (
                        <div key={k} className="contents">
                          <dt className="text-muted-foreground">{k.replace(/_/g, ' ')}</dt>
                          <dd className="break-words font-mono">{String(v)}</dd>
                        </div>
                      ))}
                  </dl>
                </div>
              )
            })}
          </div>
        </section>

        {monitoring.data && (
          <section>
            <h2 className="mb-3 text-sm font-semibold">File d’enquêtes</h2>
            <div className="flex flex-wrap gap-2">
              {Object.keys(monitoring.data.investigations).length === 0 && (
                <span className="text-sm text-muted-foreground">Vide.</span>
              )}
              {Object.entries(monitoring.data.investigations).map(([status, count]) => (
                <Badge key={status} variant="secondary" className="h-6 px-2.5">
                  {status} · {count}
                </Badge>
              ))}
            </div>
          </section>
        )}

        {me && (
          <section>
            <h2 className="mb-3 text-sm font-semibold">Votre session</h2>
            <dl className="grid max-w-md grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
              <dt className="text-muted-foreground">Identifiant</dt>
              <dd className="font-mono">{me.operator_id}</dd>
              <dt className="text-muted-foreground">Rôles</dt>
              <dd>{me.roles.join(', ') || '—'}</dd>
              <dt className="text-muted-foreground">Droits</dt>
              <dd>{me.can_act ? 'Lecture et action' : 'Lecture seule'}</dd>
              <dt className="text-muted-foreground">Périmètre TMS</dt>
              <dd>{me.tms_ids.join(', ') || 'Tous'}</dd>
            </dl>
          </section>
        )}
      </div>
    </div>
  )
}
