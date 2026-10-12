/** Client de l'API TMS (lecture seule, GET uniquement). Types alignés sur /v1/openapi.json. */

export type MissionStatus = "planned" | "in_progress" | "completed" | "cancelled"
export type VehicleStatus = "idle" | "moving" | "stopped" | "offline"
export type Severity = "low" | "medium" | "high"
export type EventType =
  | "arrived_pickup"
  | "loading_started"
  | "loading_completed"
  | "departed_pickup"
  | "arrived_delivery"
  | "unloading_started"
  | "unloading_completed"
  | "driver_message"
  | "deviation_detected"

export interface Place {
  label: string
  lat: number
  lon: number
}

export interface Point {
  lat: number
  lon: number
}

export interface Mission {
  id: string
  reference: string
  vehicle_id: string
  driver_id: string
  origin: Place
  destination: Place
  planned_route_id: string | null
  status: MissionStatus
  planned_pickup_at: string
  planned_delivery_at: string
  actual_pickup_at: string | null
  actual_delivery_at: string | null
  current_eta: string | null
  created_at: string
  updated_at: string
}

export interface MissionEvent {
  id: string
  mission_id: string
  type: EventType
  occurred_at: string
  position: Point | null
  payload: Record<string, unknown>
  source: "driver_app" | "tms"
}

export interface MissionDetail extends Mission {
  events: MissionEvent[]
}

export interface Route {
  id: string
  mission_id: string
  waypoints: { lat: number; lon: number; sequence: number }[]
  polyline: string
  distance_km: number
  duration_min: number
}

export interface Deviation {
  mission_id: string
  current_offset_km: number | null
  current_offset_since: string | null
  duration_minutes: number | null
  severity: Severity | null
  vehicle_position: Point | null
  computed_at: string | null
}

export interface Eta {
  mission_id: string
  planned_delivery_at: string
  current_eta: string | null
  delay_minutes: number | null
  remaining_distance_km: number | null
  computed_at: string
}

export interface Position {
  lat: number
  lon: number
  heading: number | null
  speed_kmh: number | null
  recorded_at: string
}

export interface PositionHistoryItem extends Position {
  mission_id: string | null
}

export interface Vehicle {
  id: string
  plate: string
  fleet_id: string | null
  current_position: Position | null
  status: VehicleStatus
  last_seen_at: string | null
  current_mission_id: string | null
}

export interface Driver {
  id: string
  name: string
  role: string
  management_level: string
  phone: string | null
  email: string | null
  status: "available" | "on_mission"
  current_mission_id: string | null
}

export interface Health {
  status: "ok"
  version: string
  time: string
  simulation: { running: boolean; run_id: string | null; speed: number }
}

export interface Page<T> {
  data: T[]
  pagination: { cursor: string | null; has_more: boolean }
}

/** Erreur RFC 7807 renvoyée par l'API. */
export class ApiError extends Error {
  readonly status: number
  readonly title: string

  constructor(status: number, title: string, detail?: string) {
    super(detail ?? title)
    this.status = status
    this.title = title
  }
}

const BASE_URL = (import.meta.env.VITE_TMS_API_URL as string | undefined) ?? ""

type Params = Record<string, string | number | string[] | undefined | null>

async function get<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue
    for (const v of Array.isArray(value) ? value : [value]) query.append(key, String(v))
  }
  const qs = query.toString()
  const res = await fetch(`${BASE_URL}/v1${path}${qs ? `?${qs}` : ""}`, { signal })
  if (!res.ok) {
    const problem = await res.json().catch(() => ({}))
    throw new ApiError(res.status, problem.title ?? res.statusText, problem.detail)
  }
  return res.json() as Promise<T>
}

/** Parcourt toutes les pages d'une liste paginée. */
async function getAll<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T[]> {
  const items: T[] = []
  let cursor: string | null = null
  do {
    const page: Page<T> = await get<Page<T>>(path, { ...params, cursor }, signal)
    items.push(...page.data)
    cursor = page.pagination.has_more ? page.pagination.cursor : null
  } while (cursor)
  return items
}

export const api = {
  health: (signal?: AbortSignal) => get<Health>("/health", {}, signal),
  missions: (status?: MissionStatus[], signal?: AbortSignal) =>
    getAll<Mission>("/missions", { status, limit: 200 }, signal),
  mission: (id: string, signal?: AbortSignal) => get<MissionDetail>(`/missions/${id}`, {}, signal),
  route: (id: string, signal?: AbortSignal) => get<Route>(`/missions/${id}/route`, {}, signal),
  deviation: (id: string, signal?: AbortSignal) => get<Deviation>(`/missions/${id}/deviation`, {}, signal),
  eta: (id: string, signal?: AbortSignal) => get<Eta>(`/missions/${id}/eta`, {}, signal),
  vehicles: (signal?: AbortSignal) => getAll<Vehicle>("/vehicles", { limit: 200 }, signal),
  vehicle: (id: string, signal?: AbortSignal) => get<Vehicle>(`/vehicles/${id}`, {}, signal),
  driver: (id: string, signal?: AbortSignal) => get<Driver>(`/drivers/${id}`, {}, signal),
  positions: (vehicleId: string, missionId: string, since?: string, signal?: AbortSignal) =>
    getAll<PositionHistoryItem>(
      `/vehicles/${vehicleId}/positions`,
      { mission_id: missionId, since, limit: 2000 },
      signal,
    ),
}
