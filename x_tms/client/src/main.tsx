import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import App from "@/App"
import "@/index.css"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Le polling est piloté par chaque requête ; on garde les données affichées entre deux lectures.
      refetchOnWindowFocus: false,
      placeholderData: (previous: unknown) => previous,
      retry: 1,
    },
  },
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
