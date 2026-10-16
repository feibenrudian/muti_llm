import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import App from "./App";
import Login from "./pages/Login";
import Providers from "./pages/Providers";
import Models from "./pages/Models";
import Pipelines from "./pages/Pipelines";
import Playground from "./pages/Playground";
import Traces from "./pages/Traces";
import TraceDetail from "./pages/TraceDetail";
import Stats from "./pages/Stats";
import Settings from "./pages/Settings";
import Users from "./pages/Users";
import UserDetail from "./pages/UserDetail";
import PortalLogin from "./pages/PortalLogin";
import PortalApp from "./pages/PortalApp";
import PortalUsage from "./pages/PortalUsage";
import PortalKeys from "./pages/PortalKeys";
import PortalTraces from "./pages/PortalTraces";
import PortalTraceDetail from "./pages/PortalTraceDetail";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
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
      { path: "users", element: <Users /> },
      { path: "users/:id", element: <UserDetail /> },
      { path: "settings", element: <Settings /> },
    ],
  },
  // 用户自助门户（T52）：与管理台平行的界面区，会话存储分 key
  { path: "/portal/login", element: <PortalLogin /> },
  {
    path: "/portal",
    element: <PortalApp />,
    children: [
      { index: true, element: <PortalUsage /> },
      { path: "keys", element: <PortalKeys /> },
      { path: "traces", element: <PortalTraces /> },
      { path: "traces/:id", element: <PortalTraceDetail /> },
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
