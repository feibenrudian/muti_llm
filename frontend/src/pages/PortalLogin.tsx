/** 门户登录页（T52）：一般用户自助门户唯一入口；成功后进入门户首页。
 *
 * 支持从管理台登录页引导跳转而来（state.username 预填）；常驻管理台入口链接。
 */

import { type FormEvent, useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { getToken } from "../lib/portalAuth";
import { Button, ErrorText, Field, Input } from "../components/ui";

export default function PortalLogin() {
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState(
    (location.state as { username?: string } | null)?.username ?? "",
  );
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  // 已持门户会话（如从根路径进站）→ 直达门户首页，不再显示登录表单
  if (getToken()) {
    return <Navigate to="/portal" replace />;
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setPending(true);
    try {
      await api.portal.auth.login(username, password);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/portal/login" ? from : "/portal", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-emerald-50 text-slate-800">
      <form
        onSubmit={submit}
        className="w-96 overflow-hidden rounded-lg border border-emerald-200 bg-white shadow-sm"
        data-testid="portal-login-form"
      >
        <div className="bg-emerald-600 px-6 py-5 text-white">
          <p className="text-2xl font-bold tracking-wide">用户门户</p>
          <p className="mt-1 text-xs text-emerald-100">多模型聚合网关 · 普通用户登录</p>
        </div>
        <div className="space-y-4 p-6">
          <ErrorText>{error}</ErrorText>
          <Field label="用户名">
            <Input
              value={username}
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
              data-testid="portal-input-username"
            />
          </Field>
          <Field label="密码">
            <Input
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
              data-testid="portal-input-password"
            />
          </Field>
          <Button
            type="submit"
            variant="success"
            disabled={!username || !password || pending}
            data-testid="portal-btn-login"
          >
            {pending ? "登录中…" : "登录"}
          </Button>
          <p className="text-center text-sm text-slate-500">
            管理员？{" "}
            <Link
              to="/login"
              className="font-medium text-slate-800 underline"
              data-testid="link-admin-login"
            >
              前往管理台登录
            </Link>
          </p>
        </div>
      </form>
    </div>
  );
}
