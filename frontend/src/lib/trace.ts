/** Trace → 时间线的纯转换与格式化（UT-27-1 / UT-30-1 覆盖）。 */

import type { TraceCall, TraceDetail } from "./api";

export type TimelineEntryKind = "request" | "call" | "judge" | "final";

export type TimelineEntry =
  | { kind: "request" }
  | { kind: "call"; call: TraceCall }
  | { kind: "judge"; calls: TraceCall[] }
  | { kind: "final" };

/**
 * 时间线：原始请求 → 裁判聚合 → 各成员/透传调用 → 最终响应。
 * 原始裁判与各次换裁判重跑（role=judge/judge_rerun）合成一组并列保留，
 * 卡片固定置于原始请求之后（对比裁判版本时无需滚过全部成员卡片）。
 */
export function toTimeline(detail: TraceDetail): TimelineEntry[] {
  const entries: TimelineEntry[] = [{ kind: "request" }];
  const judgeCalls: TraceCall[] = [];
  for (const call of detail.calls) {
    if (call.role === "judge" || call.role === "judge_rerun") {
      judgeCalls.push(call);
    } else {
      entries.push({ kind: "call", call });
    }
  }
  if (judgeCalls.length > 0) {
    entries.splice(1, 0, { kind: "judge", calls: judgeCalls });
  }
  entries.push({ kind: "final" });
  return entries;
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
