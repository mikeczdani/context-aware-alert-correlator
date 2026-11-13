/**
 * hooks/useSSEStream.ts – Redis Pub/Sub → SSE → React Query invalidáció
 *
 * Ez a hook feliratkozik a backend /api/v1/stream SSE végpontjára.
 * Amikor a backend új incidenst dolgoz fel, egy "event: update" üzenetet
 * küld, amire ez a hook meghívja a queryClient.invalidateQueries()-t.
 * A TanStack Query ezután automatikusan újralekérdezi az /incidents
 * és /metrics/summary végpontokat – a dashboard frissül anélkül,
 * hogy pollingot kellene használni.
 *
 * Adatfolyam:
 *   Backend webhook feldolgozás
 *     → Redis PUBLISH channel:sse_updates
 *       → FastAPI SSE stream (stream_routes.py)
 *         → EventSource "update" event
 *           → queryClient.invalidateQueries()
 *             → TanStack Query újralekérdez
 *               → Dashboard frissül
 */

import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'

// A backend SSE végpont URL-je
// Vite fejlesztői proxy-n keresztül éri el (vite.config.ts-ben konfigurálandó),
// így nem kell CORS-szal bajlódni fejlesztés közben
const SSE_URL = '/api/v1/stream'

// TanStack Query kulcsok – ezeket invalidáljuk SSE esemény esetén.
// Ezekkel a kulcsokkal regisztrálják a lekérdezéseiket a komponensek,
// így az invalidáció pontosan a megfelelő adatokat frissíti.
export const QUERY_KEYS = {
  incidents: ['incidents'] as const,
  metrics: ['metrics'] as const,
  topology: ['topology'] as const,
}

export function useSSEStream(): void {
  const queryClient = useQueryClient()

  useEffect(() => {
    // EventSource: a böngésző natív SSE kliens implementációja.
    // Automatikusan újracsatlakozik, ha a kapcsolat megszakad –
    // nem kell manuális reconnect logikát írni.
    const eventSource = new EventSource(SSE_URL)

    // "update" esemény: a backend új incidenst dolgozott fel
    eventSource.addEventListener('update', () => {
      // Az összes érintett lekérdezés cache-ét érvényteleníti.
      // A TanStack Query ezután a háttérben újralekérdezi ezeket,
      // és a komponensek automatikusan frissülnek az új adatokkal.
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.incidents })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.metrics })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.topology })
    })

    // "error" esemény: a backend hibaüzenetet küldött
    eventSource.addEventListener('error', (e) => {
      console.error('[SSE] Stream error:', e)
      // Az EventSource automatikusan megpróbál újracsatlakozni –
      // nem kell manuálisan kezelni
    })

    // Cleanup: ha a komponens unmountolódik (pl. navigáció),
    // lezárjuk az SSE kapcsolatot, hogy ne maradjon nyitva
    return () => {
      eventSource.close()
    }
  // queryClient referenciálisan stabil (nem változik re-render-ek között),
  // ezért nem kell a dependency array-be felvenni
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}
