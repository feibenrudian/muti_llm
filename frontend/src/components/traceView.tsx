/** Trace 详情共享展示组件（T52 抽取）：调用折叠块、裁判轮次块、成员调用 tab 卡。
 *
 * 管理台 TraceDetail 与门户 PortalTraceDetail 共用；裁判的流式重跑交互（JudgeCard）
 * 属管理台，留在页面文件。
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { TraceCall } from "../lib/api";
import { formatDuration, statusTone } from "../lib/trace";
import { Badge, Card } from "./ui";

/** 调用卡片共用的入参/输出折叠块（成员、透传与裁判的各版本同构）。 */
export function CallDetails({ call }: { call: TraceCall }) {
  return (
    <>
      <details>
        <summary className="cursor-pointer text-xs text-slate-500">入参（实际发出的完整请求）</summary>
        <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs">
          {JSON.stringify(call.request_payload, null, 2)}
        </pre>
      </details>
      {call.status === "success" ? (
        <details>
          <summary className="mt-2 cursor-pointer text-xs text-slate-500">输出</summary>
          <p className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
            {call.response_content || "（空）"}
          </p>
        </details>
      ) : (
        <p className="mt-2 rounded bg-red-50 p-2 text-sm text-red-600">{call.error_message}</p>
      )}
    </>
  );
}

/**
 * 裁判单轮折叠块：一级折叠 = 第 X 轮，二级折叠 = 输入 / 输出。
 * live（流式推进到本轮）或 errored（本轮失败落地）时自动展开，完成后保持展开；历史数据默认全折叠。
 * errored 兜底：meta 与 done 同批到达时 live 的提交帧可能被 React 批处理吞掉，失败必须可见。
 */
export function JudgeRound({
  roundNo,
  kind,
  live,
  errored = false,
  payload,
  stats,
  children,
}: {
  roundNo: number;
  kind: string;
  live: boolean;
  errored?: boolean;
  payload: TraceCall["request_payload"] | null;
  stats?: string;
  children: ReactNode;
}) {
  const roundRef = useRef<HTMLDetailsElement>(null);
  const outputRef = useRef<HTMLDetailsElement>(null);
  // 只在 live/errored 由 false 变 true 时展开，不强制收起：用户手动折叠的选择不被流式重渲染覆盖
  useEffect(() => {
    if (live || errored) {
      if (roundRef.current) roundRef.current.open = true;
      if (outputRef.current) outputRef.current.open = true;
    }
  }, [live, errored]);
  return (
    <details ref={roundRef}>
      <summary className="cursor-pointer text-sm font-medium text-slate-700">
        <span>
          第 {roundNo} 轮 · {kind}
        </span>
        {live ? "（生成中…）" : ""}
        {stats ? <span className="ml-2 text-xs font-normal text-slate-400">{stats}</span> : null}
      </summary>
      <div className="mt-1 ml-3 space-y-1 border-l border-slate-200 pl-3">
        <details>
          <summary className="cursor-pointer text-xs text-slate-500">输入</summary>
          <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs">
            {payload ? JSON.stringify(payload, null, 2) : "等待生成…"}
          </pre>
        </details>
        <details ref={outputRef}>
          <summary className="cursor-pointer text-xs text-slate-500">输出</summary>
          {children}
        </details>
      </div>
    </details>
  );
}

/** 状态小圆点的配色（tab 上概览各成员成败，沿用 statusTone 的语义）。 */
export const statusDot: Record<"success" | "warn" | "danger" | "muted", string> = {
  success: "bg-emerald-500",
  warn: "bg-amber-500",
  danger: "bg-red-500",
  muted: "bg-slate-300",
};

/**
 * 成员调用组：全部成员模型的调用信息合并为一张卡片，tab 切换查看，
 * 内容多时无需长页滚动（单成员时不渲染 tab 栏）。ICE 按 round 拆成多张卡片并带轮次标签。
 */
export function MemberCallsCard({ calls, round }: { calls: TraceCall[]; round?: number | null }) {
  const [active, setActive] = useState(0);
  // 刷新后 calls 长度可能变化，越界时回落到最后一个成员
  const index = Math.min(active, calls.length - 1);
  const call = calls[index];
  return (
    <Card
      title={calls.length > 1 ? `成员调用（${calls.length} 个）` : "成员调用"}
      actions={
        <>
          {round !== null && round !== undefined ? <Badge tone="muted">第 {round} 轮</Badge> : null}
          <span className="font-mono text-xs text-slate-500">{call.upstream_model_id}</span>
          <Badge tone={statusTone(call.status)}>{call.status}</Badge>
          <span className="text-xs text-slate-400">
            {formatDuration(call.duration_ms)} · {call.prompt_tokens}/{call.completion_tokens} tokens
          </span>
        </>
      }
    >
      {calls.length > 1 ? (
        <div role="tablist" aria-label="成员模型" className="mb-3 flex flex-wrap gap-x-4 gap-y-1 border-b border-slate-200">
          {calls.map((c, i) => (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={i === index}
              onClick={() => setActive(i)}
              className={`-mb-px inline-flex max-w-72 items-center gap-1.5 border-b-2 pb-1.5 text-xs transition-colors ${
                i === index
                  ? "border-slate-900 font-medium text-slate-900"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              <span aria-hidden className={`size-1.5 shrink-0 rounded-full ${statusDot[statusTone(c.status)]}`} />
              <span className="truncate">
                成员{i + 1} · {c.upstream_model_id}
              </span>
            </button>
          ))}
        </div>
      ) : null}
      <CallDetails call={call} />
    </Card>
  );
}
