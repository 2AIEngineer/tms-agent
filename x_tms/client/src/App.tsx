import { useCallback, useEffect, useState } from "react"

import { AppHeader } from "@/components/AppHeader"
import { FleetPanel } from "@/components/FleetPanel"
import { MissionList } from "@/components/MissionList"
import { MissionMap } from "@/components/MissionMap"
import { MissionPanel } from "@/components/MissionPanel"
import { Skeleton } from "@/components/ui/skeleton"
import {
  useActiveDeviations,
  useHealth,
  useMission,
  useMissionMetrics,
  useMissions,
  useMissionTrack,
  useRoute,
  useVehicles,
} from "@/hooks/useTms"

/** Mission sélectionnée, synchronisée avec l'URL (?mission=...) pour pouvoir partager le lien. */
function useSelectedMission() {
  const [id, setId] = useState<string | null>(() => new URLSearchParams(location.search).get("mission"))
  useEffect(() => {
    const onPop = () => setId(new URLSearchParams(location.search).get("mission"))
    window.addEventListener("popstate", onPop)
    return () => window.removeEventListener("popstate", onPop)
  }, [])
  const select = useCallback((next: string | null) => {
    setId(next)
    const url = new URL(location.href)
    if (next) url.searchParams.set("mission", next)
    else url.searchParams.delete("mission")
    history.pushState(null, "", url)
  }, [])
  return [id, select] as const
}

export default function App() {
  const [selectedId, select] = useSelectedMission()
  const health = useHealth()
  const missions = useMissions()
  const vehicles = useVehicles()
  const mission = useMission(selectedId)
  const route = useRoute(selectedId)
  const metrics = useMissionMetrics(mission.data)
  const track = useMissionTrack(mission.data)
  const deviations = useActiveDeviations(missions.data ?? [])

  // Mission disparue (ex. `tms reset`) : retour à la vue flotte.
  useEffect(() => {
    if (selectedId && mission.error && "status" in mission.error && mission.error.status === 404) select(null)
  }, [selectedId, mission.error, select])

  return (
    <div className="flex h-dvh flex-col">
      <AppHeader health={health.data} error={health.isError} />
      <main className="grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(0,14rem)_minmax(22rem,1fr)_auto] lg:grid-cols-[20rem_1fr_24rem] lg:grid-rows-1">
        <MissionList
          missions={missions.data}
          vehicles={vehicles.data ?? []}
          deviations={deviations}
          selectedId={selectedId}
          onSelect={select}
        />
        <section className="relative min-h-0">
          <MissionMap
            missions={missions.data ?? []}
            vehicles={vehicles.data ?? []}
            selected={mission.data}
            route={route.data}
            track={track}
            deviation={metrics.deviation}
            onSelect={select}
          />
        </section>
        {selectedId ? (
          mission.data ? (
            <MissionPanel
              mission={mission.data}
              route={route.data}
              deviation={metrics.deviation}
              eta={metrics.eta}
              vehicle={metrics.vehicle}
              driver={metrics.driver}
              now={health.data?.time}
            />
          ) : (
            <aside className="space-y-3 border-l p-5">
              <Skeleton className="h-6 w-40" />
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-48 w-full" />
            </aside>
          )
        ) : (
          <FleetPanel missions={missions.data ?? []} vehicles={vehicles.data ?? []} />
        )}
      </main>
    </div>
  )
}
