/** 门户壳（T52）：登录守卫 + 侧边导航 + 顶栏（当前用户/改密/登出）。
 *
 * 会话存储与管理台分 key（lib/portalAuth），可在同一浏览器并行登录两套界面。
 */

import { useState } from "react";
import { Navigate, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { getToken } from "../lib/portalAuth";
import ChangePasswordModal from "../components/ChangePasswordModal";
import { Button } from "../components/ui";

const NAV = [
  { to: "/portal", label: "用量统计", end: true },
  { to: "/portal/keys", label: "API Keys" },
  { to: "/portal/traces", label: "调用日志" },
];

export default function PortalApp() {
  const location = useLocation();
  if (!getToken()) {
    return <Navigate to="/portal/login" replace state={{ from: location.pathname }} />;
  }
  return (
    <div className="flex h-screen bg-slate-50 text-slate-800">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-white">
        <div className="border-b border-slate-100 px-4 py-4">
          <p className="text-base font-bold">muti_llm</p>
          <p className="text-xs text-slate-400">用户门户</p>
        </div>
        <nav className="flex-1 space-y-1 p-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
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
        <PortalTopBar />
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}

function PortalTopBar() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: me } = useQuery({ queryKey: ["portal-me"], queryFn: api.portal.auth.me });
  const [changeOpen, setChangeOpen] = useState(false);

  const logout = useMutation({
    mutationFn: api.portal.auth.logout,
    onSuccess: () => {
      queryClient.clear();
      navigate("/portal/login", { replace: true });
    },
  });

  return (
    <div className="border-t border-slate-100 p-2" data-testid="portal-topbar">
      <div className="flex items-center justify-between px-2 py-1">
        <span className="truncate text-xs text-slate-500" data-testid="portal-current-user">
          {me ? me.display_name || me.username : "…"}
        </span>
        <span className="flex items-center gap-1">
          <Button variant="ghost" onClick={() => setChangeOpen(true)}>
            改密
          </Button>
          <Button
            variant="ghost"
            onClick={() => logout.mutate()}
            data-testid="portal-btn-logout"
          >
            登出
          </Button>
        </span>
      </div>
      <ChangePasswordModal
        open={changeOpen}
        onClose={() => setChangeOpen(false)}
        change={api.portal.auth.changePassword}
      />
    </div>
  );
}
