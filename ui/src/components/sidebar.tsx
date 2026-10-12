import { useQuery } from '@tanstack/react-query'
import {
  Bell,
  FileText,
  Laptop,
  MessageSquarePlus,
  Moon,
  Search,
  Settings,
  Sparkles,
  Sun,
  Truck,
  Activity,
  Hourglass,
} from 'lucide-react'
import { useApp, type View } from '@/app-context'
import { useTheme } from '@/hooks/use-theme'
import { api } from '@/lib/api'
import { ACTION_LABEL, relativeTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Button } from './ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from './ui/dropdown-menu'
import { ScrollArea } from './ui/scroll-area'

const NAV: { view: View; label: string; icon: typeof Bell }[] = [
  { view: 'assistant', label: 'Assistant', icon: Sparkles },
  { view: 'alerts', label: 'Alertes', icon: Bell },
  { view: 'missions', label: 'Missions', icon: Truck },
  { view: 'reports', label: 'Rapports', icon: FileText },
  { view: 'system', label: 'Système', icon: Activity },
]

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { view, go, chat, me, newConversation, openConversation, openSettings, openPalette } = useApp()
  const { theme, setTheme } = useTheme()

  const summary = useQuery({ queryKey: ['alerts', 'summary'], queryFn: api.alertSummary, refetchInterval: 20_000, retry: false })
  const conversations = useQuery({ queryKey: ['conversations'], queryFn: api.conversations, retry: false })
  const pending = useQuery({ queryKey: ['pending'], queryFn: api.pending, refetchInterval: 20_000, retry: false })

  const open = summary.data?.open_alerts ?? 0
  const critical = (summary.data?.by_severity.critical ?? 0) > 0
  const waiting = pending.data?.pending_actions.filter((p) => p.status === 'proposed') ?? []

  const ThemeIcon = theme === 'dark' ? Moon : theme === 'light' ? Sun : Laptop

  return (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-foreground">
      <div className="flex items-center gap-2.5 px-4 pb-3 pt-4">
        <div className="flex size-8 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <Sparkles className="size-4" />
        </div>
        <div className="min-w-0 leading-tight">
          <p className="truncate text-sm font-semibold">Assistant opérateur</p>
          <p className="truncate text-xs text-muted-foreground">Suivi des missions</p>
        </div>
      </div>

      <div className="space-y-2 px-3">
        <Button
          className="w-full justify-start"
          onClick={() => {
            newConversation()
            onNavigate?.()
          }}
        >
          <MessageSquarePlus /> Nouvelle conversation
        </Button>
        <button
          onClick={() => {
            openPalette()
            onNavigate?.()
          }}
          className="flex h-8 w-full items-center gap-2 rounded-lg border bg-background/60 px-2.5 text-sm text-muted-foreground hover:bg-background"
        >
          <Search className="size-3.5" /> Rechercher…
          <kbd className="ml-auto rounded border bg-muted px-1.5 font-mono text-[10px]">Ctrl K</kbd>
        </button>
      </div>

      <nav className="mt-3 space-y-0.5 px-3" aria-label="Navigation">
        {NAV.map(({ view: v, label, icon: Icon }) => (
          <button
            key={v}
            onClick={() => {
              go(v)
              onNavigate?.()
            }}
            aria-current={view === v ? 'page' : undefined}
            className={cn(
              'flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm font-medium text-muted-foreground transition-colors hover:bg-sidebar-accent/70 hover:text-sidebar-accent-foreground',
              view === v && 'bg-sidebar-accent text-sidebar-accent-foreground',
            )}
          >
            <Icon className="size-4" />
            {label}
            {v === 'alerts' && open > 0 && (
              <span
                className={cn(
                  'ml-auto rounded-full px-1.5 text-xs tabular-nums',
                  critical ? 'bg-sev-critical text-white' : 'bg-muted text-foreground',
                )}
              >
                {open}
              </span>
            )}
          </button>
        ))}
      </nav>

      {waiting.length > 0 && (
        <div className="mx-3 mt-3 rounded-xl border border-warning/40 bg-warning/10 p-2">
          <p className="flex items-center gap-1.5 px-1.5 pb-1 text-xs font-semibold text-warning">
            <Hourglass className="size-3.5" /> À valider ({waiting.length})
          </p>
          <ul>
            {waiting.slice(0, 4).map((p) => (
              <li key={p.id}>
                <button
                  onClick={() => {
                    openConversation(p.conversation_id)
                    onNavigate?.()
                  }}
                  className="w-full rounded-lg px-1.5 py-1 text-left text-xs hover:bg-warning/15"
                >
                  <span className="font-medium">{ACTION_LABEL[p.kind]}</span>
                  <span className="line-clamp-1 text-muted-foreground">{p.summary}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-4 px-5 pb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Historique</p>
      <ScrollArea className="min-h-0 flex-1 px-3">
        <ul className="space-y-0.5 pb-3">
          {conversations.data?.conversations.length === 0 && (
            <li className="px-2.5 py-2 text-xs text-muted-foreground">Aucune conversation.</li>
          )}
          {conversations.data?.conversations.map((c) => (
            <li key={c.conversation_id}>
              <button
                onClick={() => {
                  openConversation(c.conversation_id)
                  onNavigate?.()
                }}
                className={cn(
                  'w-full rounded-lg px-2.5 py-1.5 text-left hover:bg-sidebar-accent/70',
                  chat.conversationId === c.conversation_id && view === 'assistant' && 'bg-sidebar-accent',
                )}
              >
                <span className="line-clamp-1 text-sm">{c.title || 'Sans titre'}</span>
                <span className="text-xs text-muted-foreground">{relativeTime(c.updated_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </ScrollArea>

      <div className="flex items-center gap-2 border-t px-3 py-3">
        <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-accent text-xs font-semibold uppercase text-accent-foreground">
          {(me?.name || me?.operator_id || '?').slice(0, 2)}
        </div>
        <div className="min-w-0 flex-1 leading-tight">
          <p className="truncate text-sm font-medium">{me?.name || me?.operator_id || 'Non connecté'}</p>
          <p className="truncate text-xs text-muted-foreground">{me ? (me.can_act ? 'Peut agir' : 'Lecture seule') : '—'}</p>
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" size="icon-sm" aria-label="Thème" />}>
            <ThemeIcon />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="top">
            <DropdownMenuItem onClick={() => setTheme('light')}>
              <Sun /> Clair
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('dark')}>
              <Moon /> Sombre
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('system')}>
              <Laptop /> Système
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        <Button variant="ghost" size="icon-sm" onClick={openSettings} aria-label="Réglages">
          <Settings />
        </Button>
      </div>
    </div>
  )
}
