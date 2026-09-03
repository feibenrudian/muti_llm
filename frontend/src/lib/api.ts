/** 管理 API 客户端：类型化请求 + 错误解析（UT-20-1 覆盖纯函数）。 */

export type ProviderProtocol = "openai_compatible" | "anthropic";

export interface Provider {
  id: number;
  name: string;
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
  error?: string;
}

export interface ModelRow {
  id: number;
  provider_id: number;
  display_name: string;
  upstream_model_id: string;
  default_params: Record<string, number>;
  enabled: boolean;
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
  judge_model_id: number;
  judge_prompt_template: string;
  member_timeout_seconds: number;
  fault_tolerance: Record<string, string>;
  max_concurrency: number;
  enabled: boolean;
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
    total_prompt_tokens: number;
    total_completion_tokens: number;
    created_at: string;
  };
  calls: TraceCall[];
}

export interface ServiceSettings {
  version: string;
  started_at: string;
  database_path: string;
  database_size_bytes: number;
  log_retention_days: number;
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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    throw new ApiError(resp.status, parseErrorMessage(await resp.json().catch(() => null)));
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return req<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}

function del<T>(path: string): Promise<T> {
  return req<T>(path, { method: "DELETE" });
}

export const api = {
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
  },
  settings: {
    get: () => req<ServiceSettings>("/api/admin/settings"),
    update: (body: { log_retention_days?: number }) => patch<{ ok: boolean }>("/api/admin/settings", body),
    serviceKey: () => req<ServiceKeyInfo>("/api/admin/settings/service-key"),
    resetKey: () => post<{ service_api_key: string }>("/api/admin/settings/service-key/reset"),
  },
};
