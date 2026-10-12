import { Bell, FileText, Sparkles, Truck, Zap } from 'lucide-react'
import { useApp } from '@/app-context'
import type { AssistantMode } from '@/lib/types'

const SUGGESTIONS: { icon: typeof Bell; title: string; text: string; mode?: AssistantMode }[] = [
  { icon: Bell, title: 'Alertes ouvertes', text: 'Quelles sont les alertes ouvertes les plus critiques en ce moment, et pourquoi ?' },
  { icon: Truck, title: 'Missions à risque', text: 'Quelles missions risquent d’arriver en retard aujourd’hui ?' },
  { icon: FileText, title: 'Rapport de situation', text: 'Fais-moi un rapport de situation de la flotte.', mode: 'report' },
  { icon: Zap, title: 'Immobilisations', text: 'Quels véhicules sont immobilisés depuis plus de 15 minutes ?' },
]

export function Welcome() {
  const { me, setDraft } = useApp()
  return (
    <div className="mx-auto flex max-w-2xl flex-1 flex-col items-center justify-center px-4 py-10 text-center">
      <div className="mb-5 flex size-14 items-center justify-center rounded-2xl bg-primary/12 text-primary ring-1 ring-primary/20">
        <Sparkles className="size-7" />
      </div>
      <h2 className="text-2xl font-semibold tracking-tight">
        Bonjour{me?.name ? `, ${me.name.split(' ')[0]}` : ''}.
      </h2>
      <p className="mt-2 max-w-md text-balance text-muted-foreground">
        Interrogez vos missions et vos alertes en langage naturel. L’assistant lit le TMS en direct et cite ses sources.
      </p>
      <div className="mt-8 grid w-full gap-2.5 sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s.title}
            onClick={() => setDraft({ text: s.text, mode: s.mode, nonce: Date.now() })}
            className="group flex items-start gap-3 rounded-xl border bg-card p-3.5 text-left transition-all hover:border-primary/40 hover:shadow-sm"
          >
            <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground transition-colors group-hover:bg-primary/12 group-hover:text-primary">
              <s.icon className="size-4" />
            </span>
            <span className="min-w-0">
              <span className="block text-sm font-medium">{s.title}</span>
              <span className="mt-0.5 line-clamp-2 block text-xs text-muted-foreground">{s.text}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
