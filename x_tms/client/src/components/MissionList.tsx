import { ArrowRight, Route as RouteIcon, Truck } from "lucide-react"
import { useState } from "react"

import { MissionStatusBadge, ToneBadge } from "@/components/badges"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { Deviation, Mission, Vehicle } from "@/lib/api"
import { delayTone, fmtDelay, fmtTime, minutesBetween, offsetTone, shortPlace } from "@/lib/format"
import { cn } from "@/lib/utils"

type Filter = "active" | "all"

interface Props {
  missions: Mission[] | undefined
  vehicles: Vehicle[]
  deviations: Record<string, Deviation | undefined>
  selectedId: string | null
  onSelect: (id: string | null) => void
}

export function MissionList({ missions, vehicles, deviations, selectedId, onSelect }: Props) {
  const [filter, setFilter] = useState<Filter>("active")
  const plates = Object.fromEntries(vehicles.map((v) => [v.id, v.plate]))
  const shown = (missions ?? []).filter((m) => filter === "all" || m.status === "in_progress" || m.status === "planned")
  const activeCount = (missions ?? []).filter((m) => m.status === "in_progress").length

  return (
    <aside className="flex min-h-0 flex-col border-r bg-background">
      <div className="flex items-center justify-between gap-2 border-b px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold">Missions</h2>
          <p className="text-xs text-muted-foreground">{activeCount} en cours</p>
        </div>
        <ToggleGroup
          variant="outline"
          size="sm"
          value={[filter]}
          onValueChange={(v) => v[0] && setFilter(v[0] as Filter)}
        >
          <ToggleGroupItem value="active">Actives</ToggleGroupItem>
          <ToggleGroupItem value="all">Toutes</ToggleGroupItem>
        </ToggleGroup>
      </div>

      <button
        type="button"
        onClick={() => onSelect(null)}
        className={cn(
          "flex items-center gap-2 border-b px-4 py-2.5 text-left text-sm hover:bg-muted/60",
          selectedId === null && "bg-muted font-medium",
        )}
      >
        <Truck className="size-4 text-muted-foreground" /> Vue flotte
      </button>

      <ScrollArea className="min-h-0 flex-1">
        {!missions && (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => <Skeleton key={i} className="h-20 w-full" />)}
          </div>
        )}
        {missions && shown.length === 0 && (
          <div className="p-6 text-center text-sm text-muted-foreground">
            Aucune mission {filter === "active" ? "active" : ""}.
            <p className="mt-2 font-mono text-xs">uv run tms start-missions --speed 60</p>
          </div>
        )}
        <ul>
          {shown.map((m) => {
            const delay = m.current_eta ? minutesBetween(m.planned_delivery_at, m.current_eta) : null
            const offset = deviations[m.id]?.current_offset_km
            return (
              <li key={m.id}>
                <button
                  type="button"
                  onClick={() => onSelect(m.id)}
                  className={cn(
                    "w-full border-b border-l-2 border-l-transparent px-4 py-3 text-left transition-colors hover:bg-muted/60",
                    selectedId === m.id && "border-l-blue-600 bg-blue-50/60 hover:bg-blue-50",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-sm font-medium">{m.reference}</span>
                    <MissionStatusBadge status={m.status} />
                  </div>
                  <div className="mt-1 flex items-center gap-1 text-sm">
                    <span className="truncate">{shortPlace(m.origin.label)}</span>
                    <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" />
                    <span className="truncate">{shortPlace(m.destination.label)}</span>
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                    <span className="mr-1">{plates[m.vehicle_id] ?? m.vehicle_id}</span>
                    {m.status === "in_progress" ? (
                      <>
                        <ToneBadge tone={delayTone(delay)}>ETA {fmtTime(m.current_eta)} · {fmtDelay(delay)}</ToneBadge>
                        {offset !== null && offset !== undefined && offset >= 0.5 && (
                          <ToneBadge tone={offsetTone(offset)}>
                            <RouteIcon /> {offset.toFixed(1)} km
                          </ToneBadge>
                        )}
                      </>
                    ) : (
                      <span>Livraison {fmtTime(m.actual_delivery_at ?? m.planned_delivery_at)}</span>
                    )}
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      </ScrollArea>
    </aside>
  )
}
