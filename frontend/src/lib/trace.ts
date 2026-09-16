/** Trace → 时间线的纯转换与格式化（UT-27-1 / UT-30-1 / UT-36-1 / UT-36-2 覆盖）。 */

import type { TraceCall, TraceDetail } from "./api";

export type TimelineEntryKind = "request" | "call" | "member_group" | "judge" | "final";

export type TimelineEntry =
  | { kind: "request" }
  | { kind: "call"; call: TraceCall }
  | { kind: "member_group"; round: number | null; calls: TraceCall[] }
  | { kind: "judge"; calls: TraceCall[] }
  | { kind: "final" };

/** round=null 在排序键中排最后：ICE 终局/重跑段永远位于各轮评论之后。 */
const roundSortKey = (call: TraceCall): number => call.round ?? Number.MAX_SAFE_INTEGER;

/**
 * 时间线：原始请求 → 裁判聚合 → 各成员/透传调用 → 最终响应。
 * 两段式裁判的全部行（评论 judge_critique / 最终 judge / 重跑 judge_rerun）合成一组并列保留，
 * 卡片固定置于原始请求之后（对比裁判版本时无需滚过全部成员卡片）。
 * ICE：成员行带 round 时按 round 分组成 member_group（组内保落库顺序，null 归单组）；
 * 全 null（council/透传/旧 trace）保持逐行 call 条目，视图零变化。
 * 裁判组内评论行按 round 升序，终局与重跑段（round=null）保持相对顺序排在最后。
 */
export function toTimeline(detail: TraceDetail): TimelineEntry[] {
  const entries: TimelineEntry[] = [{ kind: "request" }];
  const judgeCalls: TraceCall[] = [];
  const memberCalls: TraceCall[] = [];
  for (const call of detail.calls) {
    if (call.role === "judge" || call.role === "judge_rerun" || call.role === "judge_critique") {
      judgeCalls.push(call);
    } else if (call.role === "member") {
      memberCalls.push(call);
    } else {
      entries.push({ kind: "call", call });
    }
  }
  if (judgeCalls.length > 0) {
    // sort 稳定：同为 null 的 council 评论/终局/重跑保持原落库顺序
    judgeCalls.sort((a, b) => roundSortKey(a) - roundSortKey(b));
    entries.splice(1, 0, { kind: "judge", calls: judgeCalls });
  }
  if (memberCalls.some((call) => call.round !== null)) {
    const groups = new Map<number | null, TraceCall[]>();
    for (const call of memberCalls) {
      const group = groups.get(call.round) ?? [];
      group.push(call);
      groups.set(call.round, group);
    }
    for (const [round, calls] of groups) {
      entries.push({ kind: "member_group", round, calls });
    }
  } else {
    for (const call of memberCalls) {
      entries.push({ kind: "call", call });
    }
  }
  entries.push({ kind: "final" });
  return entries;
}

/** 裁判版本数：仅最终段行（judge/judge_rerun），评论行不计数。 */
export function judgeVersionCount(judgeCalls: TraceCall[]): number {
  return judgeCalls.filter((call) => call.role === "judge" || call.role === "judge_rerun").length;
}

/** 两段式裁判第一次调用（评论）的展示状态。 */
export interface JudgeCritique {
  status: "streaming" | "success" | "failed" | "cancelled";
  content: string;
  error: string;
  payload: TraceCall["request_payload"] | null;
  /** ICE 轮评论的轮次；council 评论与重跑评论为 null。 */
  round: number | null;
  durationMs: number;
  promptTokens: number;
  completionTokens: number;
}

/** 一个裁判版本的展示状态：已落库的行、或进行中/刚结束的流式重跑（content=第二次调用的最终答案）。 */
export interface JudgeVersion {
  status: "streaming" | "success" | "failed" | "cancelled";
  content: string;
  error: string;
  payload: TraceCall["request_payload"] | null;
  critiques: JudgeCritique[];
  durationMs: number;
  promptTokens: number;
  completionTokens: number;
  /** 流式期间已接收的增量 chunk 数（OpenAI 兼容流式下 1 chunk ≈ 1 token）。 */
  receivedTokens: number;
}

export const critiqueFromCall = (call: TraceCall): JudgeCritique => ({
  status:
    call.status === "success"
      ? "success"
      : call.status === "client_cancelled"
        ? "cancelled"
        : "failed",
  content: call.response_content,
  error: call.error_message,
  payload: call.request_payload,
  round: call.round,
  durationMs: call.duration_ms,
  promptTokens: call.prompt_tokens,
  completionTokens: call.completion_tokens,
});

export const versionFromCall = (call: TraceCall): JudgeVersion => ({
  status:
    call.status === "success"
      ? "success"
      : call.status === "client_cancelled"
        ? "cancelled"
        : "failed",
  content: call.response_content,
  error: call.error_message,
  payload: call.request_payload,
  critiques: [],
  durationMs: call.duration_ms,
  promptTokens: call.prompt_tokens,
  completionTokens: call.completion_tokens,
  receivedTokens: 0,
});

/**
 * 从已落库的裁判行建索引：原始裁判总是入表；重跑仅成功版本入表（失败的视为未生成，可重新发起）。
 * 评论配对：最终行认领其之前未被认领的同模型 critique 行——ICE 的 N 条轮评论全部归属原始裁判
 * 版本，council/重跑的"1 评论 + 1 最终"是 N=1 的特例（失败重跑同样消耗其评论行）。
 */
export function seedVersions(judges: TraceCall[]): Map<number, JudgeVersion> {
  const pendingCritiques = new Map<number, JudgeCritique[]>();
  const versions = new Map<number, JudgeVersion>();
  for (const call of judges) {
    if (call.role === "judge_critique") {
      const queue = pendingCritiques.get(call.model_id) ?? [];
      queue.push(critiqueFromCall(call));
      pendingCritiques.set(call.model_id, queue);
      continue;
    }
    if (call.role !== "judge" && call.role !== "judge_rerun") continue;
    const claimed = pendingCritiques.get(call.model_id) ?? [];
    pendingCritiques.set(call.model_id, []);
    if (call.role === "judge_rerun" && call.status !== "success") continue;
    const version = versionFromCall(call);
    version.critiques = claimed;
    versions.set(call.model_id, version);
  }
  return versions;
}

export function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
  return `${ms}ms`;
}

/** TTFT 展示：0 = 失败/未产出 token，无意义时值显示占位符。 */
export function formatFirstToken(ms: number): string {
  return ms > 0 ? formatDuration(ms) : "—";
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
