import { useQuery } from '@tanstack/react-query'
import { Menu, WifiOff } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AppContext, type AskContext, type Draft, type View } from '@/app-context'
import { Palette } from '@/components/palette'
import { SettingsDialog } from '@/components/settings-dialog'
import { Sidebar } from '@/components/sidebar'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useChat } from '@/hooks/use-chat'
import { useLiveAlerts } from '@/hooks/use-live-alerts'
import { api } from '@/lib/api'
import { AlertsView } from '@/views/alerts-view'
import { AssistantView } from '@/views/assistant-view'
import { MissionsView } from '@/views/missions-view'
import { ReportsView } from '@/views/reports-view'
import { SystemView } from '@/views/system-view'

const VIEWS: View[] = ['assistant', 'alerts', 'missions', 'reports', 'system']
const fromHash = (): View => {
  const v = location.hash.replace('#/', '') as View
  return VIEWS.includes(v) ? v : 'assistant'
}

export default function App() {
  const chat = useChat()
  const [view, setView] = useState<View>(fromHash)
  const [context, setContext] = useState<AskContext>({})
  const [draft, setDraft] = useState<Draft | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)

  const me = useQuery({ queryKey: ['me'], queryFn: api.me, retry: false, staleTime: 5 * 60_000 })

  const go = useCallback((next: View) => {
    setView(next)
    if (location.hash !== `#/${next}`) history.replaceState(null, '', `#/${next}`)
  }, [])

  useEffect(() => {
    const onHash = () => setView(fromHash())
    addEventListener('hashchange', onHash)
    return () => removeEventListener('hashchange', onHash)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((o) => !o)
      }
    }
    addEventListener('keydown', onKey)
    return () => removeEventListener('keydown', onKey)
  }, [])

  const askAbout = useCallback(
    (next: AskContext, text?: string) => {
      setContext(next)
      go('assistant')
      if (text) setDraft({ text, nonce: Date.now() })
    },
    [go],
  )

  const openConversation = useCallback(
    (id: string) => {
      go('assistant')
      void chat.load(id)
    },
    [go, chat.load],
  )

  const newConversation = useCallback(() => {
    chat.reset()
    setContext({})
    go('assistant')
  }, [chat.reset, go])

  useLiveAlerts(
    (alert) =>
      askAbout(
        { alertId: alert.id, alertTitle: alert.title, missionId: alert.mission_id, tmsId: alert.tms_id },
        'Explique cette alerte : que s’est-il passé, et que me conseilles-tu ?',
      ),
    !me.isError,
  )

  const api_ = useMemo(
    () => ({
      chat,
      me: me.data,
      view,
      go,
      context,
      setContext,
      draft,
      setDraft,
      askAbout,
      openConversation,
      newConversation,
      openSettings: () => setSettingsOpen(true),
      openPalette: () => setPaletteOpen(true),
    }),
    [chat, me.data, view, go, context, draft, askAbout, openConversation, newConversation],
  )

  return (
    <AppContext.Provider value={api_}>
      <TooltipProvider delay={200}>
        <div className="flex h-dvh overflow-hidden">
          <aside className="hidden w-72 shrink-0 border-r lg:block">
            <Sidebar />
          </aside>

          <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
            <SheetContent side="left" className="w-80 p-0" showCloseButton={false}>
              <SheetTitle className="sr-only">Navigation</SheetTitle>
              <SheetDescription className="sr-only">Menu principal</SheetDescription>
              <Sidebar onNavigate={() => setMenuOpen(false)} />
            </SheetContent>
          </Sheet>

          <main className="relative min-w-0 flex-1">
            <Button
              variant="ghost"
              size="icon"
              className="absolute left-3 top-4 z-10 lg:hidden"
              onClick={() => setMenuOpen(true)}
              aria-label="Ouvrir le menu"
            >
              <Menu />
            </Button>

            {me.isError && (
              <div className="flex items-center gap-2 border-b border-destructive/30 bg-destructive/5 px-4 py-2 text-sm">
                <WifiOff className="size-4 text-destructive" />
                <span className="min-w-0 flex-1 truncate">{(me.error as Error).message}</span>
                <Button size="xs" variant="outline" onClick={() => setSettingsOpen(true)}>
                  Réglages
                </Button>
                <Button size="xs" variant="ghost" onClick={() => void me.refetch()}>
                  Réessayer
                </Button>
              </div>
            )}

            <div className="h-full">
              {view === 'assistant' && <AssistantView />}
              {view === 'alerts' && <AlertsView />}
              {view === 'missions' && <MissionsView />}
              {view === 'reports' && <ReportsView />}
              {view === 'system' && <SystemView />}
            </div>
          </main>
        </div>

        <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
        <Palette open={paletteOpen} onOpenChange={setPaletteOpen} />
        <Toaster position="top-right" richColors closeButton />
      </TooltipProvider>
    </AppContext.Provider>
  )
}
