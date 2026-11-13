/**
 * App.tsx – Fő alkalmazás komponens
 *
 * Két feladata van:
 *   1. A QueryClientProvider becsomagolja az egész alkalmazást,
 *      így minden gyermek komponens hozzáfér a TanStack Query cache-hez.
 *   2. Az SSEListener komponens egyszer mountolódik és fenntartja
 *      az SSE kapcsolatot a backend felé – ez értesíti a Query cache-t,
 *      ha új incidens keletkezett.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useSSEStream } from './hooks/useSSEStream'
import DashboardPage from './pages/DashboardPage'

// QueryClient singleton – az egész alkalmazásban egy példány él.
// staleTime: 10s – 10 másodpercig "frissnek" tekinti az adatokat,
// nem kérdezi le újra csak azért, mert a komponens újrarenderelődött.
// Az SSE hook gondoskodik a valódi frissítésről, amikor szükséges.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 2,
    },
  },
})

// SSEListener: külön komponens, hogy a useSSEStream hook a
// QueryClientProvider-en BELÜL fusson (hozzá kell férnie a queryClient-hez)
function SSEListener() {
  useSSEStream()
  return null
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <SSEListener />
      <DashboardPage />
    </QueryClientProvider>
  )
}

export default App
