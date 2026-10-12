import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { subscribe } from '@/lib/sse'
import { SEVERITY_LABEL } from '@/lib/format'
import type { AlertRow } from '@/lib/types'

/** Écoute le flux d'alertes : un toast par nouvelle alerte et les listes se rafraîchissent. */
export function useLiveAlerts(onOpen: (alert: AlertRow) => void, enabled: boolean) {
  const queryClient = useQueryClient()
  const handler = useRef(onOpen)

  useEffect(() => {
    handler.current = onOpen
  })

  useEffect(() => {
    if (!enabled) return
    return subscribe(
      '/stream/alerts',
      ({ event, data }) => {
        if (event !== 'alert') return
        try {
          const alert = JSON.parse(data) as AlertRow
          void queryClient.invalidateQueries({ queryKey: ['alerts'] })
          const critical = alert.severity === 'critical' || alert.severity === 'high'
          ;(critical ? toast.error : toast.warning)(alert.title, {
            description: `${SEVERITY_LABEL[alert.severity]} · mission ${alert.mission_id}`,
            action: { label: 'Demander', onClick: () => handler.current(alert) },
            duration: critical ? 15_000 : 7_000,
          })
        } catch {
          /* événement illisible : ignoré */
        }
      },
      () => undefined,
    )
  }, [enabled, queryClient])
}
