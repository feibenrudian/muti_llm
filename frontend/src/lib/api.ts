/** 管理/门户 API 客户端：类型化请求 + 错误解析（UT-20-1 覆盖纯函数）。
 *
 * 认证（多用户 T46/T52）：请求按 scope 自动附带会话令牌（管理台 lib/auth / 门户 lib/portalAuth，
 * 两份 sessionStorage 互不覆盖）；401 时清除对应会话并跳对应登录页（auth 端点本身除外，
 * 登录失败由页面就地展示）。
 */

import { clearToken, getToken, setToken } from "./auth";
import {
  clearToken as clearPortalToken,
  getToken as getPortalToken,
  setToken as setPortalToken,
} from "./portalAuth";

/** 会话作用域：管理台（/api/admin/*）与自助门户（/api/portal/*）。 */
export type AuthScope = "admin" | "portal";

export type ProviderProtocol = "openai_compatible" | "anthropic";

/** 当前登录管理员（/auth/me）。 */
export interface SelfUser {
  id: number;
  username: string;
  display_name: string;
  role: string;
}

/** 登录响应：token 同时写入 sessionStorage（lib/auth），明文不在其他接口回显。 */
export interface LoginResponse {
  token: string;
  expires_at: string;
  user: SelfUser;
}

/** 用户行（多用户 T47）：key_count/last_activity_at 为聚合字段。 */
export interface UserRow {
  id: number;
  username: string;
  display_name: string;
  role: string;
  enabled: boolean;
  remark: string;
  key_count: number;
  last_activity_at: string | null;
  created_at: string;
}

/** 用户 API Key 行：is_legacy=true 为旧服务 Key 特例（只读，重置在设置页）。 */
export interface UserKeyRow {
  id: number;
  name: string;
  masked: string;
  enabled: boolean;
  is_legacy: boolean;
  created_by: number;
  last_used_at: string | null;
  created_at: string;
}

export interface UserKeyCreated extends UserKeyRow {
  /** 明文仅创建响应返回一次。 */
  api_key: string;
}

/** 授权集合（三类授权项取并集生效，需求 FR-19）。 */
export interface GrantsPayload {
  pipelines: number[];
  models: number[];
  providers: number[];
}

/** 门户"可调用范围"条目（T52）：id + 展示名。 */
export interface GrantItem {
  id: number;
  name: string;
}

export interface PortalGrants {
  pipelines: GrantItem[];
  models: GrantItem[];
  providers: GrantItem[];
}

export interface Provider {
  id: number;
  name: string;
  /** 路由命名空间的 URL 标识（/v1/route/{slug}）：创建时生成、不可改；数字 id 亦可路由。 */
  slug: string;
  protocol: ProviderProtocol;
  base_url: string;
  api_key_masked: string;
  remark: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ProviderTestResult {
  ok: boolean;
  latency_ms: number;
  models?: string[];
  synced?: string[];
  removed?: string[];
  error?: string;
}

export interface ModelRow {
  id: number;
  provider_id: number;
  display_name: string;
  upstream_model_id: string;
  default_params: Record<string, number>;
  enabled: boolean;
  /** 模型同步发现上游列表已无此 ID（联动停用；重新上架自动恢复） */
  upstream_missing: boolean;
  created_at: string;
  updated_at: string;
}

export interface PipelineMemberRow {
  id: number;
  model_id: number;
  sort_order: number;
  param_overrides: Record<string, number>;
}

export interface Pipeline {
  id: number;
  name: string;
  strategy: string;
  strategy_params: Record<string, unknown>;
  judge_model_id: number;
  judge_prompt_template: string;
  member_timeout_seconds: number;
  fault_tolerance: Record<string, string>;
  max_concurrency: number;
  enabled: boolean;
  /** 过程流式（reasoning_content）pipeline 级默认；请求体显式 stream_process 优先。 */
  stream_process: boolean;
  /** 工具聚合（T44）：开启后 pipeline 接受 tools 进入聚合语义（裁判合成最终调用）。 */
  tool_aggregation: boolean;
  members: PipelineMemberRow[];
}

export interface TraceListItem {
  id: number;
  pipeline_name: string;
  client_model_field: string;
  status: string;
  created_at: string;
  total_duration_ms: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  content_preview: string;
  /** 归属（多用户 T48）：0/"" 为历史数据。 */
  user_id: number;
  user_username: string;
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface TraceCall {
  id: number;
  role: string;
  model_id: number;
  upstream_model_id: string;
  provider_name: string;
  request_payload: {
    model: string;
    messages: ChatMessage[];
    stream?: boolean;
    temperature?: number;
    max_tokens?: number;
  };
  response_content: string;
  status: string;
  error_message: string;
  duration_ms: number;
  /** ICE 成员/评论轮次（0 起）；终局与非 ICE 行为 null。 */
  round: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  created_at: string;
}

export interface TraceDetail {
  request: {
    id: number;
    pipeline_name: string;
    client_model_field: string;
    request_messages: ChatMessage[];
    request_params: Record<string, number>;
    response_content: string;
    status: string;
    total_duration_ms: number;
    /** 请求入口 → 最终响应首个内容 token（TTFT）；失败未产出 token 时为 0。 */
    first_token_ms: number;
    total_prompt_tokens: number;
    total_completion_tokens: number;
    created_at: string;
    /** 网关校验拒绝原因（T53，status=failed 时非空）；正常链路为空。 */
    error_message: string;
    /** 归属（多用户 T48）：0/"" 为历史数据。 */
    user_id: number;
    user_username: string;
  };
  calls: TraceCall[];
}

export interface ServiceSettings {
  version: string;
  started_at: string;
  database_path: string;
  database_size_bytes: number;
  log_retention_days: number;
  /** 两类模型的对外接入开关：虚拟模型=Pipeline 名寻址；路由模型=真实模型名透传。 */
  expose_virtual_models: boolean;
  expose_routed_models: boolean;
}

export interface ServiceKeyInfo {
  available: boolean;
  masked: string;
  service_api_key: string | null;
}

export interface PlaygroundResult {
  status_code: number;
  response: {
    choices?: { message: { content: string } }[];
    degraded?: boolean;
    error?: { message: string };
  } | null;
  trace_id: number | null;
}

/** 用量统计（T41 对账看板）：prompt_tokens 恒为计费输入总量（含缓存命中/写入）。 */
export interface UsageMetrics {
  calls: number;
  failed_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

export type StatsOverview = UsageMetrics;

export interface StatsDailyRow extends UsageMetrics {
  /** UTC 日期（YYYY-MM-DD），与库内 created_at 时区一致。 */
  date: string;
}

export interface StatsProviderRow extends UsageMetrics {
  provider_name: string;
}

export interface StatsModelRow extends UsageMetrics {
  provider_name: string;
  model_id: number;
  upstream_model_id: string;
  /** 模型已删除时为 null，统计仍按日志冗余字段呈现。 */
  display_name: string | null;
}

/** 类型别名（非 interface）：需要隐式索引签名以传入 buildQuery 的 Record 参数。 */
export type StatsFilters = {
  start?: string;
  end?: string;
  provider?: string;
  model?: string;
  /** 用户筛选（多用户 T50）：数字视为 user_id，非数字视为用户名。 */
  user?: string;
};

export interface StatsUserRow extends UsageMetrics {
  user_id: number;
  /** 0/"" 为多用户之前的历史数据（前端展示"（历史）"）。 */
  user_username: string;
}

export interface StatsUserModelRow extends UsageMetrics {
  user_id: number;
  user_username: string;
  provider_name: string;
  model_id: number;
  upstream_model_id: string;
  /** 模型已删除时为 null，统计仍按日志冗余字段呈现。 */
  display_name: string | null;
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** 从 FastAPI({detail}) / OpenAI({error:{message}}) 错误体提取可读消息。 */
export function parseErrorMessage(body: unknown): string {
  if (typeof body === "string" && body) return body;
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    if (typeof record.detail === "string") return record.detail;
    if (Array.isArray(record.detail) && record.detail.length > 0) {
      const first = record.detail[0] as Record<string, unknown>;
      if (typeof first.msg === "string") return first.msg;
    }
    const error = record.error as Record<string, unknown> | undefined;
    if (error && typeof error.message === "string") return error.message;
  }
  return "请求失败";
}

/** 查询串构造：跳过空值（UT-20-1）。 */
export function buildQuery(
  params: Record<string, string | number | undefined | null>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

/**
 * 根据 base_url 自动识别 Provider 协议（UT-20-2）：
 * 主机名含 anthropic（官方 api.anthropic.com 或代理域名）→ anthropic；
 * 其余（含空值/无法解析的输入）一律按 openai_compatible 兜底。
 */
export function detectProtocol(baseUrl: string): ProviderProtocol {
  const raw = baseUrl.trim();
  if (!raw) return "openai_compatible";
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  let host: string;
  try {
    host = new URL(withScheme).hostname.toLowerCase();
  } catch {
    return "openai_compatible";
  }
  return host.includes("anthropic") ? "anthropic" : "openai_compatible";
}

const AUTH_HANDLERS: Record<AuthScope, { getToken: () => string | null; clearToken: () => void; loginPath: string }> = {
  admin: { getToken, clearToken, loginPath: "/login" },
  portal: { getToken: getPortalToken, clearToken: clearPortalToken, loginPath: "/portal/login" },
};

async function req<T>(path: string, init?: RequestInit, scope: AuthScope = "admin"): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  };
  const token = AUTH_HANDLERS[scope].getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    // 会话过期/未登录：清对应会话回对应登录页（登录接口的 401 由调用方就地处理）
    const isAuthPath = path.startsWith("/api/admin/auth/") || path.startsWith("/api/portal/auth/");
    if (resp.status === 401 && !isAuthPath) {
      AUTH_HANDLERS[scope].clearToken();
      window.location.assign(AUTH_HANDLERS[scope].loginPath);
    }
    throw new ApiError(resp.status, parseErrorMessage(await resp.json().catch(() => null)));
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/** 流式重跑的 SSE 事件：两段各一组 meta(入参)→delta*(增量)（phase=critique → final）→ done(终态与 token)。 */
export type RejudgePhase = "critique" | "final";

export type RejudgeStreamEvent =
  | { type: "meta"; phase: RejudgePhase; payload: TraceCall["request_payload"] }
  | { type: "delta"; phase: RejudgePhase; text: string }
  | {
      type: "done";
      status: string;
      critique: string;
      content: string;
      error: string;
      duration_ms: number;
      prompt_tokens: number;
      completion_tokens: number;
      /** 评论段（第一次调用）单列的耗时/token；duration_ms/prompt_tokens 为两段合计。 */
      critique_duration_ms: number;
      critique_prompt_tokens: number;
      critique_completion_tokens: number;
    };

/**
 * 换裁判重跑（流式，两段式）：逐个回调 SSE 事件，返回是否收到 done 终态
 * （连接中断收不到 done，调用方按"已中断"处理）。
 */
export async function streamRejudge(
  traceId: number | string,
  judgeModelId: number,
  onEvent: (event: RejudgeStreamEvent) => void,
): Promise<boolean> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(`/api/admin/traces/${traceId}/rejudge/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({ judge_model_id: judgeModelId }),
  });
  if (!resp.ok || !resp.body) {
    throw new ApiError(resp.status, parseErrorMessage(await resp.json().catch(() => null)));
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawDone = false;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf("\n\n");
    while (separator >= 0) {
      const block = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      separator = buffer.indexOf("\n\n");
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const event = JSON.parse(line.slice("data: ".length)) as RejudgeStreamEvent;
      if (event.type === "done") sawDone = true;
      onEvent(event);
    }
  }
  return sawDone;
}

function post<T>(path: string, body?: unknown, scope: AuthScope = "admin"): Promise<T> {
  return req<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }, scope);
}

function patch<T>(path: string, body: unknown, scope: AuthScope = "admin"): Promise<T> {
  return req<T>(path, { method: "PATCH", body: JSON.stringify(body) }, scope);
}

function del<T>(path: string, scope: AuthScope = "admin"): Promise<T> {
  return req<T>(path, { method: "DELETE" }, scope);
}

function put<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, { method: "PUT", body: JSON.stringify(body) });
}

export const api = {
  auth: {
    login: async (username: string, password: string): Promise<LoginResponse> => {
      const body = await post<LoginResponse>("/api/admin/auth/login", { username, password });
      setToken(body.token);
      return body;
    },
    logout: async (): Promise<void> => {
      try {
        await post("/api/admin/auth/logout");
      } finally {
        clearToken();
      }
    },
    me: () => req<SelfUser>("/api/admin/auth/me"),
    changePassword: (oldPassword: string, newPassword: string) =>
      post<{ ok: boolean }>("/api/admin/auth/password", {
        old_password: oldPassword,
        new_password: newPassword,
      }),
  },
  users: {
    list: (params: Record<string, string | number | undefined | null>) =>
      req<{ items: UserRow[]; total: number; page: number; page_size: number }>(
        `/api/admin/users${buildQuery(params)}`,
      ),
    create: (body: { username: string; display_name?: string; role?: string; remark?: string; password?: string }) =>
      post<UserRow>("/api/admin/users", body),
    update: (id: number, body: Record<string, unknown>) =>
      patch<UserRow>(`/api/admin/users/${id}`, body),
    remove: (id: number) => del<void>(`/api/admin/users/${id}`),
    resetPassword: (id: number, newPassword: string) =>
      post<{ ok: boolean }>(`/api/admin/users/${id}/password`, { new_password: newPassword }),
    keys: (userId: number) => req<UserKeyRow[]>(`/api/admin/users/${userId}/keys`),
    createKey: (userId: number, name: string) =>
      post<UserKeyCreated>(`/api/admin/users/${userId}/keys`, { name }),
    keySecret: (userId: number, keyId: number) =>
      req<{ api_key: string }>(`/api/admin/users/${userId}/keys/${keyId}/secret`),
    updateKey: (userId: number, keyId: number, body: { name?: string; enabled?: boolean }) =>
      patch<UserKeyRow>(`/api/admin/users/${userId}/keys/${keyId}`, body),
    deleteKey: (userId: number, keyId: number) =>
      del<void>(`/api/admin/users/${userId}/keys/${keyId}`),
    grants: (userId: number) => req<GrantsPayload>(`/api/admin/users/${userId}/grants`),
    setGrants: (userId: number, body: GrantsPayload) =>
      put<GrantsPayload>(`/api/admin/users/${userId}/grants`, body),
  },
  providers: {
    list: () => req<Provider[]>("/api/admin/providers"),
    create: (body: { name: string; protocol: ProviderProtocol; base_url: string; api_key?: string; remark?: string }) =>
      post<Provider>("/api/admin/providers", body),
    update: (id: number, body: Record<string, unknown>) => patch<Provider>(`/api/admin/providers/${id}`, body),
    remove: (id: number) => del<void>(`/api/admin/providers/${id}`),
    test: (id: number) => post<ProviderTestResult>(`/api/admin/providers/${id}/test`),
  },
  models: {
    list: () => req<ModelRow[]>("/api/admin/models"),
    create: (body: { provider_id: number; display_name: string; upstream_model_id: string; default_params?: Record<string, number> }) =>
      post<ModelRow>("/api/admin/models", body),
    update: (id: number, body: Record<string, unknown>) => patch<ModelRow>(`/api/admin/models/${id}`, body),
    remove: (id: number) => del<void>(`/api/admin/models/${id}`),
    test: (id: number) => post<{ ok: boolean; latency_ms: number; content?: string; error?: string }>(`/api/admin/models/${id}/test`),
  },
  pipelines: {
    list: () => req<Pipeline[]>("/api/admin/pipelines"),
    get: (id: number) => req<Pipeline>(`/api/admin/pipelines/${id}`),
    create: (body: Record<string, unknown>) => post<Pipeline>("/api/admin/pipelines", body),
    update: (id: number, body: Record<string, unknown>) => patch<Pipeline>(`/api/admin/pipelines/${id}`, body),
    remove: (id: number) => del<void>(`/api/admin/pipelines/${id}`),
  },
  meta: {
    judgeTemplate: () => req<{ template: string }>("/api/admin/meta/judge-template"),
    strategies: () => req<string[]>("/api/admin/meta/strategies"),
  },
  playground: {
    run: (body: { pipeline_name: string; message: string }) =>
      post<PlaygroundResult>("/api/admin/playground/run", body),
  },
  traces: {
    list: (params: Record<string, string | number | undefined | null>) =>
      req<{ items: TraceListItem[]; total: number; page: number; page_size: number }>(
        `/api/admin/traces${buildQuery(params)}`,
      ),
    get: (id: number | string) => req<TraceDetail>(`/api/admin/traces/${id}`),
    clear: () => post<{ cleared: boolean }>("/api/admin/traces/clear"),
    rejudge: (id: number | string, judgeModelId: number) =>
      post<{ call: TraceCall }>(`/api/admin/traces/${id}/rejudge`, { judge_model_id: judgeModelId }),
  },
  settings: {
    get: () => req<ServiceSettings>("/api/admin/settings"),
    update: (body: {
      log_retention_days?: number;
      expose_virtual_models?: boolean;
      expose_routed_models?: boolean;
    }) => patch<{ ok: boolean }>("/api/admin/settings", body),
    serviceKey: () => req<ServiceKeyInfo>("/api/admin/settings/service-key"),
    resetKey: () => post<{ service_api_key: string }>("/api/admin/settings/service-key/reset"),
  },
  stats: {
    overview: (params: StatsFilters) => req<StatsOverview>(`/api/admin/stats/overview${buildQuery(params)}`),
    daily: (params: StatsFilters) => req<StatsDailyRow[]>(`/api/admin/stats/daily${buildQuery(params)}`),
    byProvider: (params: StatsFilters) =>
      req<StatsProviderRow[]>(`/api/admin/stats/by-provider${buildQuery(params)}`),
    byModel: (params: StatsFilters) => req<StatsModelRow[]>(`/api/admin/stats/by-model${buildQuery(params)}`),
    byUser: (params: StatsFilters) => req<StatsUserRow[]>(`/api/admin/stats/by-user${buildQuery(params)}`),
    byUserModel: (params: StatsFilters) =>
      req<StatsUserModelRow[]>(`/api/admin/stats/by-user-model${buildQuery(params)}`),
  },
  /** 自助门户（T52）：一般用户查看自己的用量 / 管理 Key / 查自己的调用记录。 */
  portal: {
    auth: {
      login: async (username: string, password: string): Promise<LoginResponse> => {
        const body = await post<LoginResponse>("/api/portal/auth/login", { username, password }, "portal");
        setPortalToken(body.token);
        return body;
      },
      logout: async (): Promise<void> => {
        try {
          await post("/api/portal/auth/logout", undefined, "portal");
        } finally {
          clearPortalToken();
        }
      },
      me: () => req<SelfUser>("/api/portal/auth/me", undefined, "portal"),
      changePassword: (oldPassword: string, newPassword: string) =>
        post<{ ok: boolean }>(
          "/api/portal/auth/password",
          { old_password: oldPassword, new_password: newPassword },
          "portal",
        ),
    },
    keys: {
      list: () => req<UserKeyRow[]>("/api/portal/keys", undefined, "portal"),
      create: (name: string) => post<UserKeyCreated>("/api/portal/keys", { name }, "portal"),
      keySecret: (keyId: number) =>
        req<{ api_key: string }>(`/api/portal/keys/${keyId}/secret`, undefined, "portal"),
      update: (keyId: number, body: { name?: string; enabled?: boolean }) =>
        patch<UserKeyRow>(`/api/portal/keys/${keyId}`, body, "portal"),
      remove: (keyId: number) => del<void>(`/api/portal/keys/${keyId}`, "portal"),
    },
    stats: {
      overview: (params: StatsFilters) =>
        req<StatsOverview>(`/api/portal/stats/overview${buildQuery(params)}`, undefined, "portal"),
      daily: (params: StatsFilters) =>
        req<StatsDailyRow[]>(`/api/portal/stats/daily${buildQuery(params)}`, undefined, "portal"),
      byModel: (params: StatsFilters) =>
        req<StatsModelRow[]>(`/api/portal/stats/by-model${buildQuery(params)}`, undefined, "portal"),
    },
    traces: {
      list: (params: Record<string, string | number | undefined | null>) =>
        req<{ items: TraceListItem[]; total: number; page: number; page_size: number }>(
          `/api/portal/traces${buildQuery(params)}`,
          undefined,
          "portal",
        ),
      get: (id: number | string) => req<TraceDetail>(`/api/portal/traces/${id}`, undefined, "portal"),
    },
    grants: () => req<PortalGrants>("/api/portal/grants", undefined, "portal"),
  },
};
