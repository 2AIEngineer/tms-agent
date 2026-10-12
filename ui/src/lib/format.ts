import type { ActionKind, Severity } from './types'

const rtf = new Intl.RelativeTimeFormat('fr', { numeric: 'auto' })

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '—'
  const diff = (new Date(iso).getTime() - now) / 1000
  const abs = Math.abs(diff)
  if (abs < 45) return "à l'instant"
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour')
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), 'day')
  return new Date(iso).toLocaleDateString('fr-FR')
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: 'short' })
}

export function clock(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })
}

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: 'Critique',
  high: 'Élevée',
  medium: 'Moyenne',
  low: 'Faible',
}

export const SEVERITY_ORDER: Severity[] = ['critical', 'high', 'medium', 'low']

export const ACTION_LABEL: Record<ActionKind, string> = {
  close_alert: "Clore l'alerte",
  call_driver: 'Appeler le chauffeur',
  log_decision: 'Consigner une décision',
}

export const CATEGORY_LABEL: Record<string, string> = {
  route_deviation: "Écart d'itinéraire",
  delay: 'Retard',
  immobilization: 'Immobilisation',
  data_gap: 'Données manquantes',
  safety: 'Sécurité',
}

const TOOL_LABEL: Record<string, string> = {
  list_tms: 'Liste des TMS',
  get_tms_time: "Heure du TMS",
  get_active_missions: 'Missions actives',
  get_mission_snapshot: "État d'une mission",
  get_mission_events: "Événements d'une mission",
  get_position_history: 'Historique de positions',
  get_route: 'Itinéraire',
  get_driver: 'Chauffeur',
  get_vehicle: 'Véhicule',
  close_alert: "Clore l'alerte",
  call_driver: 'Appeler le chauffeur',
  log_decision: 'Consigner une décision',
}

export const toolLabel = (tool: string) => TOOL_LABEL[tool] ?? tool.replace(/_/g, ' ')

export const formatTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)} k` : String(n))

export function missionIdOf(m: { mission_id?: string; id?: string }): string {
  return String(m.mission_id ?? m.id ?? '')
}
