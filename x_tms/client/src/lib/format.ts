import type { EventType, MissionStatus, Severity, VehicleStatus } from "@/lib/api"

const timeFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" })
const timeSecFmt = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
const dateTimeFmt = new Intl.DateTimeFormat("fr-FR", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
})

export const fmtTime = (iso: string | null | undefined) => (iso ? timeFmt.format(new Date(iso)) : "—")
export const fmtTimeSec = (iso: string | null | undefined) => (iso ? timeSecFmt.format(new Date(iso)) : "—")
export const fmtDateTime = (iso: string | null | undefined) => (iso ? dateTimeFmt.format(new Date(iso)) : "—")

/** Minutes écoulées entre deux instants ISO (b - a). */
export function minutesBetween(a: string, b: string): number {
  return (new Date(b).getTime() - new Date(a).getTime()) / 60000
}

export function fmtDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—"
  const m = Math.round(Math.abs(minutes))
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  const rest = m % 60
  return rest ? `${h} h ${String(rest).padStart(2, "0")}` : `${h} h`
}

export function fmtDelay(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined) return "—"
  if (Math.abs(minutes) < 1) return "à l'heure"
  return `${minutes > 0 ? "+" : "−"}${fmtDuration(minutes)}`
}

export type Tone = "ok" | "warn" | "bad" | "neutral"

export function delayTone(minutes: number | null | undefined): Tone {
  if (minutes === null || minutes === undefined) return "neutral"
  if (minutes > 45) return "bad"
  if (minutes > 10) return "warn"
  return "ok"
}

export function offsetTone(km: number | null | undefined): Tone {
  if (km === null || km === undefined) return "neutral"
  if (km >= 5) return "bad"
  if (km >= 0.5) return "warn"
  return "ok"
}

export const MISSION_STATUS_LABEL: Record<MissionStatus, string> = {
  planned: "Planifiée",
  in_progress: "En cours",
  completed: "Terminée",
  cancelled: "Annulée",
}

export const VEHICLE_STATUS_LABEL: Record<VehicleStatus, string> = {
  idle: "Au repos",
  moving: "En mouvement",
  stopped: "À l'arrêt",
  offline: "Hors ligne",
}

export const SEVERITY_LABEL: Record<Severity, string> = { low: "faible", medium: "moyenne", high: "élevée" }

export const EVENT_LABEL: Record<EventType, string> = {
  arrived_pickup: "Arrivée au chargement",
  loading_started: "Début du chargement",
  loading_completed: "Chargement terminé",
  departed_pickup: "Départ du chargement",
  arrived_delivery: "Arrivée à la livraison",
  unloading_started: "Début du déchargement",
  unloading_completed: "Déchargement terminé",
  driver_message: "Message chauffeur",
  deviation_detected: "Dérive détectée",
}

/** Libellé court d'un lieu ("Entrepôt Casablanca" -> "Casablanca"). */
export function shortPlace(label: string): string {
  return label.replace(/^(Entrepôt|Plateforme|Client)\s+/i, "")
}
