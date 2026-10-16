/** 应用壳（多用户 T46）：登录守卫 + 侧边导航 + 顶栏（当前用户/改密/登出）。 */

import { useState } from "react";
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./lib/api";
import { getToken } from "./lib/auth";
import ChangePasswordModal from "./components/ChangePasswordModal";
import { Button } from "./components/ui";

const NAV = [
  { to: "/providers", label: "供应商" },
  { to: "/models", label: "模型" },
  { to: "/pipelines", label: "组合（Pipeline）" },
  { to: "/playground", label: "试运行" },
  { to: "/traces", label: "调用日志" },
  { to: "/stats", label: "用量统计" },
  { to: "/users", label: "用户" },
  { to: "/settings", label: "设置" },
];

export default function App() {
  const location = useLocation();
  // 已登录但令牌失效时由 api.ts 统一清会话并跳登录页；此处只挡"无令牌"的首次进入。
  // 未登录进站（根路径）默认到用户门户登录页（T52）；直接访问管理页仍到管理台登录。
  if (!getToken()) {
    if (location.pathname === "/") {
      return <Navigate to="/portal/login" replace />;
    }
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return (
    <div className="flex h-screen bg-slate-50 text-slate-800">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-4 py-4">
          <p className="text-base font-bold">muti_llm</p>
          <p className="text-xs text-slate-400">多模型聚合网关</p>
        </div>
        <nav className="flex-1 space-y-1 p-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm ${
                  isActive ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <TopBar />
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}

function TopBar() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: me } = useQuery({ queryKey: ["auth-me"], queryFn: api.auth.me });
  const [changeOpen, setChangeOpen] = useState(false);

  const logout = useMutation({
    mutationFn: api.auth.logout,
    onSuccess: () => {
      queryClient.clear();
      navigate("/login", { replace: true });
    },
  });

  return (
    <div className="border-t border-slate-100 p-2" data-testid="topbar">
      <div className="flex items-center justify-between px-2 py-1">
        <span className="truncate text-xs text-slate-500" data-testid="current-user">
          {me ? me.display_name || me.username : "…"}
        </span>
        <span className="flex items-center gap-1">
          <Button variant="ghost" onClick={() => setChangeOpen(true)}>
            改密
          </Button>
          <Button
            variant="ghost"
            onClick={() => logout.mutate()}
            data-testid="btn-logout"
          >
            登出
          </Button>
        </span>
      </div>
      <ChangePasswordModal
        open={changeOpen}
        onClose={() => setChangeOpen(false)}
        change={api.auth.changePassword}
      />
    </div>
  );
}
