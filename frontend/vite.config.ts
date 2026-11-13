import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// A backend címe – felülírható a VITE_API_TARGET környezeti változóval,
// pl. ha a backend nem localhostról, hanem Dockerből vagy WSL-ből fut.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // A frontend relatív /api/v1/... URL-eket hív (incidentsApi.ts) és
    // megnyit egy SSE streamet (useSSEStream.ts). Dev módban a Vite szerver
    // ezeket a kéréseket a backendre proxyzza, így nincs CORS-gond és a
    // böngésző szemszögéből minden egy originről (a Vite portról) jön.
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        // Az SSE stream (text/event-stream) hosszan nyitva marad – a proxy
        // nem pufferelheti, különben a frontend sosem kapná meg az eseményeket.
        // A http-proxy alapból streameli a választ; a WebSocket-flag nem kell,
        // de a timeout kikapcsolása biztosítja, hogy a nyitott SSE kapcsolatot
        // ne vágja el inaktivitás miatt.
        proxyTimeout: 0,
        timeout: 0,
      },
    },
  },
})
