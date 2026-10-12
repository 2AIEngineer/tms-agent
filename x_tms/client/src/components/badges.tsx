import type { ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import type { MissionStatus, VehicleStatus } from "@/lib/api"
import { MISSION_STATUS_LABEL, VEHICLE_STATUS_LABEL, type Tone } from "@/lib/format"
import { cn } from "@/lib/utils"

const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warn: "bg-amber-50 text-amber-800 border-amber-200",
  bad: "bg-red-50 text-red-700 border-red-200",
  neutral: "bg-muted text-muted-foreground border-transparent",
}

export function ToneBadge({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <Badge variant="outline" className={cn(TONE_CLASS[tone], "tabular-nums", className)}>
      {children}
    </Badge>
  )
}

const MISSION_TONE: Record<MissionStatus, string> = {
  planned: "bg-slate-100 text-slate-700",
  in_progress: "bg-blue-600 text-white",
  completed: "bg-emerald-100 text-emerald-800",
  cancelled: "bg-zinc-200 text-zinc-600 line-through",
}

export function MissionStatusBadge({ status }: { status: MissionStatus }) {
  return <Badge className={cn("border-transparent", MISSION_TONE[status])}>{MISSION_STATUS_LABEL[status]}</Badge>
}

const VEHICLE_DOT: Record<VehicleStatus, string> = {
  moving: "bg-emerald-500",
  stopped: "bg-amber-500",
  offline: "bg-zinc-400",
  idle: "bg-slate-300",
}

export function VehicleStatusDot({ status, withLabel = false }: { status: VehicleStatus; withLabel?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={cn("size-2 rounded-full", VEHICLE_DOT[status], status === "moving" && "animate-pulse")} />
      {withLabel && <span>{VEHICLE_STATUS_LABEL[status]}</span>}
    </span>
  )
}
