import { useQuery } from '@tanstack/react-query'
import { Activity, Bell, FileText, MessageSquarePlus, Moon, Settings, Sparkles, Sun, Truck } from 'lucide-react'
import { useApp } from '@/app-context'
import { useTheme } from '@/hooks/use-theme'
import { api } from '@/lib/api'
import { SeverityDot } from './severity'
import {
  CommandDialog,
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from './ui/command'

export function Palette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const app = useApp()
  const { setTheme } = useTheme()
  const conversations = useQuery({ queryKey: ['conversations'], queryFn: api.conversations, enabled: open, retry: false })
  const alerts = useQuery({
    queryKey: ['alerts', 'picker'],
    queryFn: () => api.alerts({ status: 'open', limit: 30 }),
    enabled: open,
    retry: false,
  })

  const run = (fn: () => void) => () => {
    onOpenChange(false)
    fn()
  }

  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Rechercher" description="Naviguer, retrouver une conversation ou une alerte">
      <Command>
        <CommandInput placeholder="Aller à, rechercher une conversation, une alerte…" />
        <CommandList>
          <CommandEmpty>Aucun résultat.</CommandEmpty>
          <CommandGroup heading="Actions">
            <CommandItem onSelect={run(app.newConversation)}>
              <MessageSquarePlus /> Nouvelle conversation
            </CommandItem>
            <CommandItem onSelect={run(app.openSettings)}>
              <Settings /> Réglages de connexion
            </CommandItem>
            <CommandItem onSelect={run(() => setTheme('dark'))}>
              <Moon /> Thème sombre
            </CommandItem>
            <CommandItem onSelect={run(() => setTheme('light'))}>
              <Sun /> Thème clair
            </CommandItem>
          </CommandGroup>
          <CommandSeparator />
          <CommandGroup heading="Aller à">
            <CommandItem onSelect={run(() => app.go('assistant'))}>
              <Sparkles /> Assistant
            </CommandItem>
            <CommandItem onSelect={run(() => app.go('alerts'))}>
              <Bell /> Alertes
            </CommandItem>
            <CommandItem onSelect={run(() => app.go('missions'))}>
              <Truck /> Missions
            </CommandItem>
            <CommandItem onSelect={run(() => app.go('reports'))}>
              <FileText /> Rapports
            </CommandItem>
            <CommandItem onSelect={run(() => app.go('system'))}>
              <Activity /> Système
            </CommandItem>
          </CommandGroup>
          {conversations.data && conversations.data.conversations.length > 0 && (
            <CommandGroup heading="Conversations">
              {conversations.data.conversations.map((c) => (
                <CommandItem
                  key={c.conversation_id}
                  value={`conversation ${c.title ?? ''} ${c.conversation_id}`}
                  onSelect={run(() => app.openConversation(c.conversation_id))}
                >
                  <Sparkles /> {c.title || 'Sans titre'}
                </CommandItem>
              ))}
            </CommandGroup>
          )}
          {alerts.data && alerts.data.alerts.length > 0 && (
            <CommandGroup heading="Alertes ouvertes">
              {alerts.data.alerts.map((a) => (
                <CommandItem
                  key={a.id}
                  value={`alerte ${a.title} ${a.mission_id}`}
                  onSelect={run(() =>
                    app.askAbout(
                      { alertId: a.id, alertTitle: a.title, missionId: a.mission_id, tmsId: a.tms_id },
                      'Explique cette alerte : que s’est-il passé, et que me conseilles-tu ?',
                    ),
                  )}
                >
                  <SeverityDot severity={a.severity} /> {a.title}
                  <span className="ml-auto font-mono text-xs text-muted-foreground">{a.mission_id}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          )}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
