/** Trace → 时间线的纯转换与格式化（UT-27-1 覆盖）。 */

import type { TraceCall, TraceDetail } from "./api";

export type TimelineEntryKind = "request" | "call" | "final";

export interface TimelineEntry {
  kind: TimelineEntryKind;
  call?: TraceCall;
}

/** 时间线：原始请求 → 各上游调用（成员/裁判/透传，按后端给定顺序）→ 最终响应。 */
export function toTimeline(detail: TraceDetail): TimelineEntry[] {
  return [
    { kind: "request" },
    ...detail.calls.map((call) => ({ kind: "call" as const, call })),
    { kind: "final" },
  ];
}

export function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

export function statusTone(status: string): "success" | "warn" | "danger" | "muted" {
  if (status === "success") return "success";
  if (status === "degraded") return "warn";
  if (status === "failed") return "danger";
  return "muted"; // client_cancelled / pending 等
}
