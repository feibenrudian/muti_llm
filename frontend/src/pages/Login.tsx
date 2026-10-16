/** 登录页（多用户 T46）：管理台唯一入口；成功后回跳来源页或供应商页。
 *
 * 一般用户在此登录会得到 403（密码正确但角色不符）：就地展示"前往用户门户登录"引导
 * （T52），并常驻门户入口链接。
 */

import { type FormEvent, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { Button, ErrorText, Field, Input } from "../components/ui";

export default function Login() {
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [needPortal, setNeedPortal] = useState(false);
  const [pending, setPending] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setNeedPortal(false);
    setPending(true);
    try {
      await api.auth.login(username, password);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/login" ? from : "/providers", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      // 密码正确但角色为普通用户（403）：引导去门户登录（T52）
      setNeedPortal(err instanceof ApiError && err.status === 403);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex h-screen items-center justify-center bg-slate-100 text-slate-800">
      <form
        onSubmit={submit}
        className="w-96 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm"
        data-testid="login-form"
      >
        <div className="bg-slate-800 px-6 py-5 text-white">
          <p className="text-2xl font-bold tracking-wide">管理台</p>
          <p className="mt-1 text-xs text-slate-300">多模型聚合网关 · 管理员登录</p>
        </div>
        <div className="space-y-4 p-6">
          <ErrorText>{error}</ErrorText>
          {needPortal ? (
            <Button
              variant="secondary"
              onClick={() => navigate("/portal/login", { state: { username } })}
              data-testid="btn-go-portal"
            >
              前往用户门户登录 →
            </Button>
          ) : null}
          <Field label="用户名">
            <Input
              value={username}
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
              data-testid="input-username"
            />
          </Field>
          <Field label="密码">
            <Input
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
              data-testid="input-password"
            />
          </Field>
          <Button
            type="submit"
            disabled={!username || !password || pending}
            data-testid="btn-login"
          >
            {pending ? "登录中…" : "登录"}
          </Button>
          <p className="text-center text-sm text-slate-500">
            普通用户？{" "}
            <Link
              to="/portal/login"
              className="font-medium text-emerald-700 underline"
              data-testid="link-portal-login"
            >
              前往用户门户登录
            </Link>
          </p>
        </div>
      </form>
    </div>
  );
}
