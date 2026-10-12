import { useQuery } from '@tanstack/react-query'
import { FileText, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { useApp } from '@/app-context'
import { Markdown } from '@/components/markdown'
import { EmptyState, ErrorNotice, PageHeader } from '@/components/page'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { dateTime } from '@/lib/format'
import { cn } from '@/lib/utils'

export function ReportsView() {
  const { askAbout, setDraft } = useApp()
  const reports = useQuery({ queryKey: ['reports'], queryFn: api.reports, retry: false })
  const [selected, setSelected] = useState<string | null>(null)
  const items = reports.data?.reports ?? []
  const current = items.find((r) => r.conversation_id === selected) ?? items[0]

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="Rapports"
        description="Rapports de situation produits à heure fixe. Ils sont conservés ici et ne sont jamais notifiés."
        actions={
          <>
            <Button
              size="sm"
              onClick={() => {
                askAbout({})
                setDraft({ text: 'Fais-moi un rapport de situation de la flotte.', mode: 'report', nonce: Date.now() })
              }}
            >
              <FileText /> Générer un rapport
            </Button>
            <Button variant="outline" size="icon-sm" onClick={() => void reports.refetch()} aria-label="Actualiser">
              <RefreshCw className={cn(reports.isFetching && 'animate-spin')} />
            </Button>
          </>
        }
      />
      {reports.isLoading && <Skeleton className="m-6 h-60" />}
      {reports.error && <ErrorNotice error={reports.error} onRetry={() => void reports.refetch()} />}
      {reports.data && items.length === 0 && (
        <EmptyState
          icon={<FileText />}
          title="Aucun rapport"
          description="Les rapports planifiés apparaîtront ici dès leur production par le service agent."
        />
      )}
      {items.length > 0 && (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <ul className="max-h-48 shrink-0 divide-y overflow-y-auto border-b md:max-h-none md:w-64 md:border-b-0 md:border-r">
            {items.map((r) => (
              <li key={r.conversation_id}>
                <button
                  onClick={() => setSelected(r.conversation_id)}
                  className={cn(
                    'w-full px-4 py-3 text-left text-sm hover:bg-muted/50',
                    current?.conversation_id === r.conversation_id && 'bg-accent/60',
                  )}
                >
                  <p className="font-medium">{dateTime(r.produced_at)}</p>
                  <p className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
                    {(r.report ?? '').replace(/[#*_>`-]/g, '').slice(0, 120)}
                  </p>
                </button>
              </li>
            ))}
          </ul>
          <article className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-3xl px-4 py-6 sm:px-6">
              {current?.report ? (
                <Markdown>{current.report}</Markdown>
              ) : (
                <p className="text-sm text-muted-foreground">Ce rapport est vide.</p>
              )}
            </div>
          </article>
        </div>
      )}
    </div>
  )
}
