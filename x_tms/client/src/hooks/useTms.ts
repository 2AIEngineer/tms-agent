import { useQueries, useQuery } from "@tanstack/react-query"
import { useEffect, useRef, useState } from "react"

import { api, type Mission, type MissionStatus, type PositionHistoryItem } from "@/lib/api"

/** Intervalle de polling (ms). Le TMS ne pousse rien : le client interroge l'API. */
export const POLL_MS = Number(import.meta.env.VITE_POLL_MS ?? 2000)

const isActive = (status?: MissionStatus) => status === "planned" || status === "in_progress"

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: POLL_MS,
    retry: false,
  })
}

export function useMissions() {
  return useQuery({
    queryKey: ["missions"],
    queryFn: ({ signal }) => api.missions(undefined, signal),
    refetchInterval: POLL_MS,
  })
}

export function useMission(id: string | null) {
  return useQuery({
    queryKey: ["mission", id],
    queryFn: ({ signal }) => api.mission(id!, signal),
    enabled: !!id,
    refetchInterval: (q) => (isActive(q.state.data?.status) ? POLL_MS : false),
  })
}

export function useRoute(id: string | null) {
  return useQuery({
    queryKey: ["route", id],
    queryFn: ({ signal }) => api.route(id!, signal),
    enabled: !!id,
    staleTime: Infinity, // l'itinéraire planifié ne change pas
  })
}

export function useMissionMetrics(mission: Mission | undefined) {
  const active = isActive(mission?.status)
  const id = mission?.id ?? null
  const interval = active ? POLL_MS : false
  const deviation = useQuery({
    queryKey: ["deviation", id],
    queryFn: ({ signal }) => api.deviation(id!, signal),
    enabled: !!id,
    refetchInterval: interval,
  })
  const eta = useQuery({
    queryKey: ["eta", id],
    queryFn: ({ signal }) => api.eta(id!, signal),
    enabled: !!id,
    refetchInterval: interval,
  })
  const vehicle = useQuery({
    queryKey: ["vehicle", mission?.vehicle_id],
    queryFn: ({ signal }) => api.vehicle(mission!.vehicle_id, signal),
    enabled: !!mission,
    refetchInterval: interval,
  })
  const driver = useQuery({
    queryKey: ["driver", mission?.driver_id],
    queryFn: ({ signal }) => api.driver(mission!.driver_id, signal),
    enabled: !!mission,
    refetchInterval: active ? POLL_MS * 5 : false,
  })
  return { deviation: deviation.data, eta: eta.data, vehicle: vehicle.data, driver: driver.data }
}

export function useVehicles() {
  return useQuery({
    queryKey: ["vehicles"],
    queryFn: ({ signal }) => api.vehicles(signal),
    refetchInterval: POLL_MS,
  })
}

/** Écarts à l'itinéraire des missions en cours (indicateurs de la liste). */
export function useActiveDeviations(missions: Mission[]) {
  const active = missions.filter((m) => m.status === "in_progress")
  const results = useQueries({
    queries: active.map((m) => ({
      queryKey: ["deviation", m.id],
      queryFn: ({ signal }: { signal: AbortSignal }) => api.deviation(m.id, signal),
      refetchInterval: POLL_MS,
    })),
  })
  return Object.fromEntries(active.map((m, i) => [m.id, results[i]?.data]))
}

/**
 * Trace GPS d'une mission : chargement complet puis récupération incrémentale
 * des nouvelles positions (`since` = dernière position reçue).
 */
export function useMissionTrack(mission: Mission | undefined) {
  const [track, setTrack] = useState<{ missionId: string | null; points: PositionHistoryItem[] }>({
    missionId: null,
    points: [],
  })
  // Copie de travail pour le polling (lue uniquement dans l'effet).
  const store = useRef<{ missionId: string | null; points: PositionHistoryItem[] }>({ missionId: null, points: [] })
  const missionId = mission?.id
  const vehicleId = mission?.vehicle_id
  const active = isActive(mission?.status)

  useEffect(() => {
    if (!missionId || !vehicleId) return
    if (store.current.missionId !== missionId) {
      store.current = { missionId, points: [] }
      setTrack({ missionId, points: [] })
    }
    const ctrl = new AbortController()
    let inFlight = false

    const pull = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const since = store.current.points.at(-1)?.recorded_at
        const fresh = await api.positions(vehicleId, missionId, since, ctrl.signal)
        if (fresh.length && store.current.missionId === missionId) {
          store.current = { missionId, points: [...store.current.points, ...fresh] }
          setTrack(store.current)
        }
      } catch {
        // erreur réseau ou annulation : nouvelle tentative au prochain cycle
      } finally {
        inFlight = false
      }
    }

    void pull()
    // Une mission terminée n'a plus de nouvelles positions : une dernière lecture suffit.
    const timer = active ? window.setInterval(pull, POLL_MS) : undefined
    return () => {
      ctrl.abort()
      window.clearInterval(timer)
    }
  }, [missionId, vehicleId, active])

  return track.missionId === missionId ? track.points : []
}
