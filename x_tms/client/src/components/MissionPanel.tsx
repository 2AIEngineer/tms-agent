import {
  AlertTriangle,
  ArrowDown,
  CheckCircle2,
  Clock,
  MessageSquareWarning,
  Navigation,
  PackageCheck,
  PackageOpen,
  Phone,
  Route as RouteIcon,
  Truck,
  UserRound,
  type LucideIcon,
} from "lucide-react"
import type { ReactNode } from "react"

import { MissionStatusBadge, ToneBadge, VehicleStatusDot } from "@/components/badges"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import type { Deviation, Driver, Eta, EventType, MissionDetail, MissionEvent, Route, Vehicle } from "@/lib/api"
import {
  delayTone,
  EVENT_LABEL,
  fmtDateTime,
  fmtDelay,
  fmtDuration,
  fmtTime,
  minutesBetween,
  offsetTone,
  SEVERITY_LABEL,
  type Tone,
} from "@/lib/format"
import { cn } from "@/lib/utils"

interface Props {
  mission: MissionDetail
  route?: Route
  deviation?: Deviation
  eta?: Eta
  vehicle?: Vehicle
  driver?: Driver
  now?: string // heure du TMS
}

const STEPS = [
  { key: "approach", label: "Approche" },
  { key: "loading", label: "Chargement" },
  { key: "transit", label: "Trajet" },
  { key: "delivery", label: "Livraison" },
  { key: "done", label: "Terminée" },
] as const

function currentStep(m: MissionDetail): number {
  const has = (t: EventType) => m.events.some((e) => e.type === t)
  if (m.status === "completed" || has("unloading_completed")) return 4
  if (has("arrived_delivery")) return 3
  if (has("departed_pickup")) return 2
  if (has("arrived_pickup")) return 1
  return 0
}

const EVENT_ICON: Partial<Record<EventType, LucideIcon>> = {
  arrived_pickup: PackageOpen,
  loading_completed: PackageCheck,
  departed_pickup: Navigation,
  arrived_delivery: PackageOpen,
  unloading_completed: CheckCircle2,
  driver_message: MessageSquareWarning,
  deviation_detected: AlertTriangle,
}

export function MissionPanel({ mission, route, deviation, eta, vehicle, driver, now }: Props) {
  const step = currentStep(mission)
  const cancelled = mission.status === "cancelled"
  const lastEvent = mission.events.at(-1)
  const lastEventAge = lastEvent && now ? minutesBetween(lastEvent.occurred_at, now) : null
  const active = mission.status === "in_progress"
  const remaining = eta?.remaining_distance_km
  const progress = route && remaining !== null && remaining !== undefined
    ? Math.max(0, Math.min(1, 1 - remaining / route.distance_km))
    : 0
  const lastSeenAge = vehicle?.last_seen_at && now ? minutesBetween(vehicle.last_seen_at, now) : null

  return (
    <aside className="flex min-h-0 flex-col border-l bg-background">
      <div className="space-y-3 border-b px-5 py-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-mono text-base font-semibold">{mission.reference}</h2>
          <MissionStatusBadge status={mission.status} />
        </div>
        <div className="space-y-0.5 text-sm">
          <PlaceLine letter="A" className="bg-blue-600" label={mission.origin.label} />
          <ArrowDown className="ml-1.5 size-3 text-muted-foreground" />
          <PlaceLine letter="B" className="bg-emerald-600" label={mission.destination.label} />
        </div>
        {!cancelled && <Stepper step={step} />}
        {route && (
          <div>
            <div className="mb-1 flex justify-between text-xs text-muted-foreground">
              <span>{(route.distance_km * progress).toFixed(0)} / {route.distance_km.toFixed(0)} km</span>
              <span>{Math.round(progress * 100)} %</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full bg-blue-600 transition-all duration-700" style={{ width: `${progress * 100}%` }} />
            </div>
          </div>
        )}
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-5 px-5 py-4">
          <div className="grid grid-cols-2 gap-3">
            <Kpi icon={Clock} title="ETA livraison" tone={delayTone(eta?.delay_minutes)}>
              <div className="text-lg font-semibold tabular-nums">{fmtTime(eta?.current_eta)}</div>
              <ToneBadge tone={delayTone(eta?.delay_minutes)}>{fmtDelay(eta?.delay_minutes)}</ToneBadge>
              <Hint>Prévue {fmtTime(mission.planned_delivery_at)}</Hint>
            </Kpi>

            <Kpi icon={RouteIcon} title="Écart itinéraire" tone={offsetTone(deviation?.current_offset_km)}>
              {deviation?.current_offset_km === null || deviation?.current_offset_km === undefined ? (
                <>
                  <div className="text-lg font-semibold">—</div>
                  <Hint>Calculé après le départ</Hint>
                </>
              ) : (
                <>
                  <div className="text-lg font-semibold tabular-nums">{deviation.current_offset_km.toFixed(1)} km</div>
                  {deviation.current_offset_since ? (
                    <ToneBadge tone={offsetTone(deviation.current_offset_km)}>
                      depuis {fmtDuration(deviation.duration_minutes)}
                    </ToneBadge>
                  ) : (
                    <ToneBadge tone="ok">sur l'itinéraire</ToneBadge>
                  )}
                  {deviation.severity && <Hint>Sévérité {SEVERITY_LABEL[deviation.severity]}</Hint>}
                </>
              )}
            </Kpi>

            <Kpi icon={Truck} title="Véhicule">
              {vehicle ? (
                <>
                  <div className="text-lg font-semibold tabular-nums">
                    {Math.round(vehicle.current_position?.speed_kmh ?? 0)} <span className="text-sm font-normal">km/h</span>
                  </div>
                  <div className="text-xs"><VehicleStatusDot status={vehicle.status} withLabel /></div>
                  <Hint>
                    {vehicle.plate}
                    {lastSeenAge !== null && ` · vu il y a ${lastSeenAge < 1 ? "<1 min" : fmtDuration(lastSeenAge)}`}
                  </Hint>
                </>
              ) : (
                <div className="text-lg font-semibold">—</div>
              )}
            </Kpi>

            <Kpi
              icon={AlertTriangle}
              title="Dernier événement"
              tone={active && lastEventAge !== null ? (lastEventAge > 120 ? "bad" : lastEventAge > 60 ? "warn" : "neutral") : "neutral"}
            >
              <div className="text-lg font-semibold tabular-nums">
                {lastEventAge === null ? "—" : `il y a ${fmtDuration(lastEventAge)}`}
              </div>
              <Hint>{lastEvent ? EVENT_LABEL[lastEvent.type] : "Aucun événement"}</Hint>
            </Kpi>
          </div>

          {driver && (
            <Section title="Chauffeur">
              <div className="flex items-center gap-3 text-sm">
                <div className="flex size-9 items-center justify-center rounded-full bg-muted">
                  <UserRound className="size-4 text-muted-foreground" />
                </div>
                <div className="min-w-0">
                  <div className="font-medium">{driver.name}</div>
                  <div className="flex items-center gap-1 text-xs text-muted-foreground">
                    <Phone className="size-3" /> {driver.phone ?? "—"}
                  </div>
                </div>
              </div>
            </Section>
          )}

          <Section title="Planning">
            <dl className="grid grid-cols-[auto_1fr_1fr] gap-x-4 gap-y-1.5 text-sm">
              <span />
              <dt className="text-xs text-muted-foreground">Prévu</dt>
              <dt className="text-xs text-muted-foreground">Réel</dt>
              <dt className="text-muted-foreground">Enlèvement</dt>
              <dd className="tabular-nums">{fmtDateTime(mission.planned_pickup_at)}</dd>
              <dd className="tabular-nums">{fmtDateTime(mission.actual_pickup_at)}</dd>
              <dt className="text-muted-foreground">Livraison</dt>
              <dd className="tabular-nums">{fmtDateTime(mission.planned_delivery_at)}</dd>
              <dd className="tabular-nums">
                {mission.actual_delivery_at ? fmtDateTime(mission.actual_delivery_at) : (
                  <span className="text-muted-foreground">ETA {fmtTime(mission.current_eta)}</span>
                )}
              </dd>
            </dl>
          </Section>

          <Section title={`Événements (${mission.events.length})`}>
            {mission.events.length === 0 ? (
              <p className="text-sm text-muted-foreground">Aucun événement pour l'instant.</p>
            ) : (
              <ol className="relative space-y-3 border-l pl-5">
                {[...mission.events].reverse().map((e) => <TimelineItem key={e.id} event={e} />)}
              </ol>
            )}
          </Section>
        </div>
      </ScrollArea>
    </aside>
  )
}

function PlaceLine({ letter, label, className }: { letter: string; label: string; className: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={cn("flex size-4 items-center justify-center rounded-full text-[10px] font-bold text-white", className)}>
        {letter}
      </span>
      <span className="truncate">{label}</span>
    </div>
  )
}

function Stepper({ step }: { step: number }) {
  return (
    <ol className="flex items-center gap-1">
      {STEPS.map((s, i) => (
        <li key={s.key} className="flex flex-1 flex-col gap-1">
          <div className={cn("h-1 rounded-full", i < step ? "bg-blue-600" : i === step ? "bg-blue-300" : "bg-muted")} />
          <span className={cn("text-[11px]", i === step ? "font-medium text-foreground" : "text-muted-foreground")}>
            {s.label}
          </span>
        </li>
      ))}
    </ol>
  )
}

const KPI_TONE: Record<Tone, string> = {
  ok: "",
  neutral: "",
  warn: "border-amber-300 bg-amber-50/40",
  bad: "border-red-300 bg-red-50/50",
}

function Kpi({ icon: Icon, title, tone = "neutral", children }: { icon: LucideIcon; title: string; tone?: Tone; children: ReactNode }) {
  return (
    <div className={cn("flex flex-col items-start gap-1 rounded-lg border p-3 transition-colors", KPI_TONE[tone])}>
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className="size-3.5" /> {title}
      </div>
      {children}
    </div>
  )
}

function Hint({ children }: { children: ReactNode }) {
  return <div className="text-xs text-muted-foreground">{children}</div>
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      {children}
      <Separator className="mt-5" />
    </section>
  )
}

function TimelineItem({ event }: { event: MissionEvent }) {
  const Icon = EVENT_ICON[event.type] ?? CheckCircle2
  const critical = event.type === "driver_message" && event.payload.severity === "critical"
  const text = typeof event.payload.text === "string" ? event.payload.text : null
  const weight = typeof event.payload.weight_kg === "number" ? event.payload.weight_kg : null
  return (
    <li className="relative">
      <span
        className={cn(
          "absolute top-0.5 -left-[29px] flex size-4.5 items-center justify-center rounded-full border bg-background",
          critical ? "border-red-400 text-red-600" : event.type === "driver_message" ? "border-amber-400 text-amber-600" : "text-blue-600",
        )}
      >
        <Icon className="size-3" />
      </span>
      <div className="flex items-baseline justify-between gap-2">
        <span className={cn("text-sm font-medium", critical && "text-red-700")}>{EVENT_LABEL[event.type]}</span>
        <time className="shrink-0 text-xs text-muted-foreground tabular-nums">{fmtDateTime(event.occurred_at)}</time>
      </div>
      {text && <p className={cn("mt-0.5 text-sm", critical ? "text-red-700" : "text-muted-foreground")}>{text}</p>}
      {weight !== null && <p className="mt-0.5 text-xs text-muted-foreground">{weight.toLocaleString("fr-FR")} kg chargés</p>}
      <p className="text-[11px] text-muted-foreground/80">source : {event.source === "driver_app" ? "appli chauffeur" : "TMS"}</p>
    </li>
  )
}
