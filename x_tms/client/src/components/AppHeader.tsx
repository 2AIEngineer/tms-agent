import { Clock, Gauge, Radio } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import type { Health } from "@/lib/api"
import { POLL_MS } from "@/hooks/useTms"
import { fmtTimeSec } from "@/lib/format"
import { cn } from "@/lib/utils"

const dateFmt = new Intl.DateTimeFormat("fr-FR", { weekday: "short", day: "2-digit", month: "short" })

export function AppHeader({ health, error }: { health?: Health; error: boolean }) {
  const sim = health?.simulation
  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-background px-4">
      <div className="flex items-center gap-3">
        <div className="flex size-8 items-center justify-center rounded-md bg-blue-600 text-sm font-bold text-white">T</div>
        <div className="leading-tight">
          <h1 className="text-sm font-semibold">Suivi des missions</h1>
          <p className="text-xs text-muted-foreground">TMS · visualisation temps réel</p>
        </div>
      </div>
      <div className="flex items-center gap-3 text-sm">
        {sim?.running && (
          <Badge variant="outline" className="gap-1 border-blue-200 bg-blue-50 text-blue-700">
            <Gauge /> Simulation ×{sim.speed}
          </Badge>
        )}
        <div className="flex items-center gap-1.5 tabular-nums" title="Heure du TMS (accélérée pendant une simulation)">
          <Clock className="size-4 text-muted-foreground" />
          {health ? (
            <>
              <span className="text-muted-foreground">{dateFmt.format(new Date(health.time))}</span>
              <span className="font-medium">{fmtTimeSec(health.time)}</span>
            </>
          ) : (
            "—"
          )}
        </div>
        <div
          className={cn("flex items-center gap-1.5 text-xs", error ? "text-red-600" : "text-muted-foreground")}
          title={`Lecture seule — l'API est interrogée toutes les ${POLL_MS / 1000} s`}
        >
          <Radio className={cn("size-4", error ? "text-red-600" : "text-emerald-600")} />
          {error ? "API injoignable" : `Polling ${POLL_MS / 1000} s`}
        </div>
      </div>
    </header>
  )
}
