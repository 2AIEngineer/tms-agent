import polyline from "@mapbox/polyline"
import * as maplibregl from "maplibre-gl"
import type { GeoJSONSource, Map as MlMap } from "maplibre-gl"
import "maplibre-gl/dist/maplibre-gl.css"
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url"
import { Crosshair, Maximize2 } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { Button } from "@/components/ui/button"
import type { Deviation, Mission, MissionDetail, PositionHistoryItem, Route, Vehicle } from "@/lib/api"
import { EVENT_LABEL, fmtDateTime, shortPlace, VEHICLE_STATUS_LABEL } from "@/lib/format"
import { boundsOf, distanceToPathKm, nearestPointOnPath, type LngLat } from "@/lib/geo"

// MapLibre localise son worker relativement à son propre fichier, ce que le bundling de Vite casse :
// on laisse Vite construire le worker et on lui donne son URL.
maplibregl.setWorkerUrl(maplibreWorkerUrl)

const OFF_ROUTE_KM = 0.5
const EMPTY: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] }

const OSM_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.35 } }],
}

const EVENT_COLORS: Record<string, string> = {
  driver_message: "#d97706",
  deviation_detected: "#dc2626",
  arrived_pickup: "#2563eb",
  loading_started: "#2563eb",
  loading_completed: "#2563eb",
  departed_pickup: "#2563eb",
  arrived_delivery: "#16a34a",
  unloading_started: "#16a34a",
  unloading_completed: "#16a34a",
}

const ARROW_SVG =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 2 19 21 12 17 5 21Z"/></svg>'

function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!)
}

function pinElement(letter: string, kind: "origin" | "destination", title: string) {
  const el = document.createElement("div")
  el.className = `place-pin place-pin--${kind}`
  el.textContent = letter
  el.title = title
  return el
}

interface Props {
  missions: Mission[]
  vehicles: Vehicle[]
  selected?: MissionDetail
  route?: Route
  track: PositionHistoryItem[]
  deviation?: Deviation
  onSelect: (missionId: string) => void
}

export function MissionMap({ missions, vehicles, selected, route, track, deviation, onSelect }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MlMap | null>(null)
  const [ready, setReady] = useState(false)
  const [follow, setFollow] = useState(false)
  const vehicleMarkers = useRef(new Map<string, maplibregl.Marker>())
  const placeMarkers = useRef<maplibregl.Marker[]>([])
  const fittedFor = useRef<string | null>(null)
  // Cache des écarts déjà calculés (clé : itinéraire + horodatage de la position).
  const [offsetCache] = useState(() => new Map<string, number>())
  const onSelectRef = useRef(onSelect)
  useEffect(() => {
    onSelectRef.current = onSelect
  }, [onSelect])

  // --- Initialisation de la carte -------------------------------------------------
  useEffect(() => {
    const map = new maplibregl.Map({
      container: container.current!,
      style: OSM_STYLE,
      center: [-6.8, 33.4],
      zoom: 6,
      attributionControl: { compact: true },
    })
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-left")
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right")
    map.on("load", () => {
      map.addSource("route", { type: "geojson", data: EMPTY })
      map.addSource("trace", { type: "geojson", data: EMPTY })
      map.addSource("deviation-link", { type: "geojson", data: EMPTY })
      map.addSource("events", { type: "geojson", data: EMPTY })
      map.addLayer({
        id: "route-casing", type: "line", source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#ffffff", "line-width": 9, "line-opacity": 0.9 },
      })
      map.addLayer({
        id: "route", type: "line", source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#93c5fd", "line-width": 5 },
      })
      map.addLayer({
        id: "trace", type: "line", source: "trace",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-width": ["match", ["get", "kind"], "approach", 3, 4],
          "line-color": ["match", ["get", "kind"], "off", "#dc2626", "approach", "#64748b", "#1d4ed8"],
          "line-dasharray": ["match", ["get", "kind"], "approach", ["literal", [1, 1.5]], ["literal", [1, 0]]],
        },
      })
      map.addLayer({
        id: "deviation-link", type: "line", source: "deviation-link",
        paint: { "line-color": "#dc2626", "line-width": 2, "line-dasharray": [2, 2] },
      })
      map.addLayer({
        id: "events", type: "circle", source: "events",
        paint: {
          "circle-radius": 6,
          "circle-color": ["get", "color"],
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 2,
        },
      })
      map.on("click", "events", (e) => {
        const f = e.features?.[0]
        if (!f || f.geometry.type !== "Point") return
        const p = f.properties as { label: string; time: string; text: string }
        new maplibregl.Popup({ offset: 10, closeButton: false })
          .setLngLat(f.geometry.coordinates as LngLat)
          .setHTML(
            `<div class="event-popup"><strong>${escapeHtml(p.label)}</strong><span>${escapeHtml(p.time)}</span>${
              p.text ? `<p>${escapeHtml(p.text)}</p>` : ""
            }</div>`,
          )
          .addTo(map)
      })
      map.on("mouseenter", "events", () => (map.getCanvas().style.cursor = "pointer"))
      map.on("mouseleave", "events", () => (map.getCanvas().style.cursor = ""))
      setReady(true)
    })
    mapRef.current = map
    const resize = new ResizeObserver(() => map.resize())
    resize.observe(container.current!)
    const markers = vehicleMarkers.current
    return () => {
      resize.disconnect()
      markers.clear()
      map.remove()
      mapRef.current = null
    }
  }, [])

  // --- Données dérivées ------------------------------------------------------------
  const routeCoords = useMemo<LngLat[]>(
    () => (route ? polyline.decode(route.polyline).map(([lat, lon]) => [lon, lat] as LngLat) : []),
    [route],
  )

  const departedAt = selected?.events.find((e) => e.type === "departed_pickup")?.occurred_at
  const traceGeojson = useMemo<GeoJSON.FeatureCollection>(() => {
    if (!track.length) return EMPTY
    if (offsetCache.size > 5000) offsetCache.clear()
    const kindOf = (p: PositionHistoryItem) => {
      if (!departedAt || p.recorded_at < departedAt || !routeCoords.length) return "approach"
      const key = `${route?.id}:${p.recorded_at}`
      let d = offsetCache.get(key)
      if (d === undefined) {
        d = distanceToPathKm([p.lon, p.lat], routeCoords)
        offsetCache.set(key, d)
      }
      return d > OFF_ROUTE_KM ? "off" : "on"
    }
    // Découpe la trace en segments homogènes (approche / sur l'itinéraire / hors itinéraire).
    const features: GeoJSON.Feature[] = []
    let kind = kindOf(track[0])
    let coords: LngLat[] = [[track[0].lon, track[0].lat]]
    for (const p of track.slice(1)) {
      const k = kindOf(p)
      const c: LngLat = [p.lon, p.lat]
      coords.push(c)
      if (k !== kind) {
        features.push({ type: "Feature", properties: { kind }, geometry: { type: "LineString", coordinates: coords } })
        kind = k
        coords = [c]
      }
    }
    if (coords.length > 1) {
      features.push({ type: "Feature", properties: { kind }, geometry: { type: "LineString", coordinates: coords } })
    }
    return { type: "FeatureCollection", features }
  }, [track, routeCoords, departedAt, route?.id, offsetCache])

  const selectedVehicle = vehicles.find((v) => v.id === selected?.vehicle_id)

  // --- Mise à jour des couches -----------------------------------------------------
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const set = (id: string, data: GeoJSON.FeatureCollection) => (map.getSource(id) as GeoJSONSource).setData(data)

    set(
      "route",
      routeCoords.length
        ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: routeCoords } }] }
        : EMPTY,
    )
    set("trace", selected ? traceGeojson : EMPTY)
    set("events", {
      type: "FeatureCollection",
      features: (selected?.events ?? [])
        .filter((e) => e.position)
        .map((e) => ({
          type: "Feature",
          properties: {
            label: EVENT_LABEL[e.type],
            time: fmtDateTime(e.occurred_at),
            text: typeof e.payload.text === "string" ? e.payload.text : "",
            color: EVENT_COLORS[e.type] ?? "#475569",
          },
          geometry: { type: "Point", coordinates: [e.position!.lon, e.position!.lat] },
        })),
    })

    // Lien véhicule -> itinéraire pendant une dérive.
    const pos = selectedVehicle?.current_position
    const offset = deviation?.current_offset_km ?? 0
    const nearest = pos && offset > OFF_ROUTE_KM ? nearestPointOnPath([pos.lon, pos.lat], routeCoords) : null
    set(
      "deviation-link",
      nearest && pos
        ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [[pos.lon, pos.lat], nearest] } }] }
        : EMPTY,
    )
  }, [ready, selected, routeCoords, traceGeojson, deviation, selectedVehicle])

  // --- Origine / destination de la mission sélectionnée ----------------------------
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    placeMarkers.current.forEach((m) => m.remove())
    placeMarkers.current = []
    if (!selected) return
    const { origin, destination } = selected
    placeMarkers.current = [
      new maplibregl.Marker({ element: pinElement("A", "origin", origin.label), anchor: "bottom" })
        .setLngLat([origin.lon, origin.lat]).addTo(map),
      new maplibregl.Marker({ element: pinElement("B", "destination", destination.label), anchor: "bottom" })
        .setLngLat([destination.lon, destination.lat]).addTo(map),
    ]
  }, [ready, selected?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // --- Véhicules (tous les véhicules en mission) -----------------------------------
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const missionById = new Map(missions.map((m) => [m.id, m]))
    const shown = vehicles.filter(
      (v) => v.current_position && (v.current_mission_id || v.id === selected?.vehicle_id),
    )
    const keep = new Set(shown.map((v) => v.id))
    for (const [id, marker] of vehicleMarkers.current) {
      if (!keep.has(id)) {
        marker.remove()
        vehicleMarkers.current.delete(id)
      }
    }
    for (const v of shown) {
      const pos = v.current_position!
      let marker = vehicleMarkers.current.get(v.id)
      if (!marker) {
        const el = document.createElement("button")
        el.type = "button"
        el.className = "vehicle-marker"
        el.innerHTML = `<span class="vm-arrow">${ARROW_SVG}</span><span class="vm-label"></span>`
        marker = new maplibregl.Marker({ element: el }).setLngLat([pos.lon, pos.lat]).addTo(map)
        vehicleMarkers.current.set(v.id, marker)
      }
      const el = marker.getElement()
      const missionId = v.current_mission_id ?? (v.id === selected?.vehicle_id ? selected.id : null)
      const mission = missionId ? missionById.get(missionId) : undefined
      el.dataset.status = v.status
      el.dataset.selected = String(v.id === selected?.vehicle_id)
      el.onclick = (ev) => {
        ev.stopPropagation()
        if (missionId) onSelectRef.current(missionId)
      }
      el.title = `${v.plate} · ${VEHICLE_STATUS_LABEL[v.status]}${
        mission ? ` · ${mission.reference} (${shortPlace(mission.origin.label)} → ${shortPlace(mission.destination.label)})` : ""
      }`
      el.querySelector<HTMLElement>(".vm-arrow")!.style.transform = `rotate(${pos.heading ?? 0}deg)`
      el.querySelector<HTMLElement>(".vm-label")!.textContent = mission?.reference ?? v.plate
      marker.setLngLat([pos.lon, pos.lat])
    }
  }, [ready, vehicles, missions, selected])

  // --- Cadrage -----------------------------------------------------------------------
  const fitSelection = () => {
    const map = mapRef.current
    if (!map) return
    const points: LngLat[] = selected
      ? [...routeCoords, ...track.map((p) => [p.lon, p.lat] as LngLat)]
      : vehicles.filter((v) => v.current_mission_id && v.current_position).map((v) => [v.current_position!.lon, v.current_position!.lat] as LngLat)
    const bounds = boundsOf(points)
    if (bounds) map.fitBounds(bounds, { padding: 60, maxZoom: 12, duration: 600 })
  }

  useEffect(() => {
    if (!ready) return
    const key = selected ? (route ? selected.id : null) : vehicles.length ? "fleet" : null
    if (key && fittedFor.current !== key) {
      fittedFor.current = key
      fitSelection()
    }
  }, [ready, selected?.id, route, vehicles.length]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const pos = selectedVehicle?.current_position
    if (follow && pos && mapRef.current) mapRef.current.easeTo({ center: [pos.lon, pos.lat], duration: 500 })
  }, [follow, selectedVehicle?.current_position])

  return (
    <div className="relative h-full w-full">
      {/* .maplibregl-map impose `position: relative` (CSS non-layered, prioritaire sur Tailwind) :
          le conteneur de la carte prend donc 100 % d'un parent positionné. */}
      <div className="absolute inset-0">
        <div ref={container} className="h-full w-full" />
      </div>
      <div className="absolute top-3 right-3 flex gap-2">
        {selected && (
          <Button size="sm" variant={follow ? "default" : "secondary"} onClick={() => setFollow((f) => !f)} className="shadow">
            <Crosshair /> {follow ? "Suivi actif" : "Suivre le véhicule"}
          </Button>
        )}
        <Button size="sm" variant="secondary" onClick={fitSelection} className="shadow">
          <Maximize2 /> Recadrer
        </Button>
      </div>
      {selected && (
        <div className="absolute bottom-8 left-3 rounded-md border bg-background/95 px-3 py-2 text-xs shadow-sm">
          <LegendLine color="#93c5fd" width={5} label="Itinéraire planifié" />
          <LegendLine color="#1d4ed8" label="Trajet réalisé" />
          <LegendLine color="#dc2626" label={`Hors itinéraire (> ${OFF_ROUTE_KM} km)`} />
          <LegendLine color="#64748b" dashed label="Approche du chargement" />
        </div>
      )}
    </div>
  )
}

function LegendLine({ color, label, width = 3, dashed = false }: { color: string; label: string; width?: number; dashed?: boolean }) {
  return (
    <div className="flex items-center gap-2 py-0.5">
      <span className="inline-block w-6" style={{ borderTop: `${width}px ${dashed ? "dashed" : "solid"} ${color}` }} />
      <span>{label}</span>
    </div>
  )
}
