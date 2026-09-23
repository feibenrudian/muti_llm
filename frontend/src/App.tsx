import { NavLink, Outlet } from "react-router-dom";

const NAV = [
  { to: "/providers", label: "供应商" },
  { to: "/models", label: "模型" },
  { to: "/pipelines", label: "组合（Pipeline）" },
  { to: "/playground", label: "试运行" },
  { to: "/traces", label: "调用日志" },
  { to: "/stats", label: "用量统计" },
  { to: "/settings", label: "设置" },
];

export default function App() {
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
      </aside>
      <main className="flex-1 overflow-auto p-6">
        <Outlet />
      </main>
    </div>
  );
}
