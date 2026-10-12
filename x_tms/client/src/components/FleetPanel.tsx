import { MousePointerClick } from "lucide-react"

import { VehicleStatusDot } from "@/components/badges"
import type { Mission, Vehicle, VehicleStatus } from "@/lib/api"
import { VEHICLE_STATUS_LABEL } from "@/lib/format"

const ORDER: VehicleStatus[] = ["moving", "stopped", "offline", "idle"]

export function FleetPanel({ missions, vehicles }: { missions: Mission[]; vehicles: Vehicle[] }) {
  const count = (s: string) => missions.filter((m) => m.status === s).length
  return (
    <aside className="flex min-h-0 flex-col gap-5 overflow-y-auto border-l bg-background px-5 py-4">
      <div>
        <h2 className="text-base font-semibold">Vue flotte</h2>
        <p className="text-sm text-muted-foreground">Tous les véhicules en mission, mis à jour en continu.</p>
      </div>
      <div className="grid grid-cols-3 gap-2 text-center">
        <Stat label="En cours" value={count("in_progress")} />
        <Stat label="Terminées" value={count("completed")} />
        <Stat label="Annulées" value={count("cancelled")} />
      </div>
      <div>
        <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">Véhicules</h3>
        <ul className="space-y-1.5 text-sm">
          {[...vehicles]
            .sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status))
            .map((v) => (
              <li key={v.id} className="flex items-center justify-between gap-2 rounded-md border px-3 py-2">
                <span className="font-mono">{v.plate}</span>
                <span className="flex items-center gap-2 text-xs text-muted-foreground">
                  {v.status === "moving" && v.current_position && `${Math.round(v.current_position.speed_kmh ?? 0)} km/h`}
                  <VehicleStatusDot status={v.status} />
                  {VEHICLE_STATUS_LABEL[v.status]}
                </span>
              </li>
            ))}
        </ul>
      </div>
      <div className="flex items-start gap-2 rounded-md bg-muted/60 p-3 text-sm text-muted-foreground">
        <MousePointerClick className="mt-0.5 size-4 shrink-0" />
        Sélectionnez une mission dans la liste ou cliquez sur un véhicule de la carte pour la suivre en détail.
      </div>
    </aside>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      <div className="text-xs text-muted-foreground">{label}</div>
    </div>
  )
}
