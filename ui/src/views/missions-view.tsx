import { useQuery } from '@tanstack/react-query'
import { MessageSquareText, RefreshCw, Search, Truck } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useApp } from '@/app-context'
import { EmptyState, ErrorNotice, PageHeader } from '@/components/page'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { dateTime, missionIdOf } from '@/lib/format'
import { cn } from '@/lib/utils'

const str = (v: unknown) => (v === null || v === undefined || v === '' ? null : String(v))

export function MissionsView() {
  const { askAbout } = useApp()
  const [filter, setFilter] = useState('')
  const missions = useQuery({ queryKey: ['missions'], queryFn: api.missions, refetchInterval: 30_000, retry: false })
  const rows = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    return (missions.data?.missions ?? []).filter((m) =>
      needle ? JSON.stringify(m).toLowerCase().includes(needle) : true,
    )
  }, [missions.data, filter])
  const failures = Object.entries(missions.data?.unreadable_tms ?? {})

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Missions"
        description="Missions surveillées. Choisissez-en une pour interroger l’assistant à son sujet."
        actions={
          <Button variant="outline" size="sm" onClick={() => void missions.refetch()}>
            <RefreshCw className={cn(missions.isFetching && 'animate-spin')} /> Actualiser
          </Button>
        }
      />
      <div className="border-b px-4 py-3 sm:px-6">
        <div className="relative max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filtrer…" className="pl-8" />
        </div>
        {failures.map(([tms, reason]) => (
          <p key={tms} className="mt-2 text-sm text-destructive">
            TMS « {tms} » illisible : {reason}
          </p>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {missions.isLoading && <Skeleton className="m-6 h-40" />}
        {missions.error && <ErrorNotice error={missions.error} onRetry={() => void missions.refetch()} />}
        {missions.data && rows.length === 0 && (
          <EmptyState
            icon={<Truck />}
            title="Aucune mission"
            description={filter ? 'Aucun résultat pour ce filtre.' : 'Aucune mission planifiée ou en cours.'}
          />
        )}
        {rows.length > 0 && (
          <ul className="grid gap-3 p-4 sm:grid-cols-2 sm:p-6 xl:grid-cols-3">
            {rows.map((m) => {
              const id = missionIdOf(m)
              return (
                <li key={`${m.tms_id}/${id}`} className="flex flex-col gap-3 rounded-2xl border bg-card p-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-sm font-medium">{str(m.reference) ?? id}</p>
                      <p className="text-xs text-muted-foreground">{m.tms_id}</p>
                    </div>
                    {str(m.status) && <Badge variant="secondary">{str(m.status)}</Badge>}
                  </div>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                    {str(m.vehicle_id) && (
                      <>
                        <dt className="text-muted-foreground">Véhicule</dt>
                        <dd className="font-mono">{str(m.vehicle_id)}</dd>
                      </>
                    )}
                    {str(m.driver_id) && (
                      <>
                        <dt className="text-muted-foreground">Chauffeur</dt>
                        <dd className="font-mono">{str(m.driver_id)}</dd>
                      </>
                    )}
                    {str(m.planned_delivery_at) && (
                      <>
                        <dt className="text-muted-foreground">Livraison prévue</dt>
                        <dd>{dateTime(str(m.planned_delivery_at))}</dd>
                      </>
                    )}
                  </dl>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-auto self-start"
                    onClick={() =>
                      askAbout({ missionId: id, tmsId: m.tms_id }, 'Où en est cette mission, et y a-t-il un risque ?')
                    }
                  >
                    <MessageSquareText /> Interroger l’assistant
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
