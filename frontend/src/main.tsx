import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import App from "./App";
import Providers from "./pages/Providers";
import Models from "./pages/Models";
import Pipelines from "./pages/Pipelines";
import Playground from "./pages/Playground";
import Traces from "./pages/Traces";
import TraceDetail from "./pages/TraceDetail";
import Stats from "./pages/Stats";
import Settings from "./pages/Settings";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Providers /> },
      { path: "providers", element: <Providers /> },
      { path: "models", element: <Models /> },
      { path: "pipelines", element: <Pipelines /> },
      { path: "playground", element: <Playground /> },
      { path: "traces", element: <Traces /> },
      { path: "traces/:id", element: <TraceDetail /> },
      { path: "stats", element: <Stats /> },
      { path: "settings", element: <Settings /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
