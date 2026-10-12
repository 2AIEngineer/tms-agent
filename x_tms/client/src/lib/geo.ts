/** Distance d'un point à un tracé (km), approximation équirectangulaire locale. */
export type LngLat = [number, number]

export function distanceToPathKm(p: LngLat, path: LngLat[]): number {
  let best = Infinity
  for (let i = 0; i < path.length - 1; i++) {
    const [aLon, aLat] = path[i]
    const [bLon, bLat] = path[i + 1]
    const kx = Math.cos((((aLat + bLat) / 2) * Math.PI) / 180) * 111.32
    const ky = 110.574
    const ax = aLon * kx, ay = aLat * ky
    const dx = bLon * kx - ax, dy = bLat * ky - ay
    const px = p[0] * kx, py = p[1] * ky
    const seg2 = dx * dx + dy * dy
    const t = seg2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / seg2))
    const d = Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
    if (d < best) best = d
  }
  return best
}

export function boundsOf(points: LngLat[]): [LngLat, LngLat] | null {
  if (!points.length) return null
  let [minLon, minLat] = points[0]
  let [maxLon, maxLat] = points[0]
  for (const [lon, lat] of points) {
    minLon = Math.min(minLon, lon); maxLon = Math.max(maxLon, lon)
    minLat = Math.min(minLat, lat); maxLat = Math.max(maxLat, lat)
  }
  return [[minLon, minLat], [maxLon, maxLat]]
}

/** Point du tracé le plus proche de `p`. */
export function nearestPointOnPath(p: LngLat, path: LngLat[]): LngLat | null {
  let best: { d: number; q: LngLat } | null = null
  for (let i = 0; i < path.length - 1; i++) {
    const [aLon, aLat] = path[i]
    const [bLon, bLat] = path[i + 1]
    const kx = Math.cos((((aLat + bLat) / 2) * Math.PI) / 180) * 111.32
    const ky = 110.574
    const ax = aLon * kx, ay = aLat * ky
    const dx = bLon * kx - ax, dy = bLat * ky - ay
    const px = p[0] * kx, py = p[1] * ky
    const seg2 = dx * dx + dy * dy
    const t = seg2 === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / seg2))
    const qx = ax + t * dx, qy = ay + t * dy
    const d = Math.hypot(px - qx, py - qy)
    if (!best || d < best.d) best = { d, q: [qx / kx, qy / ky] }
  }
  return best?.q ?? null
}
