import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, streamRejudge, type ModelRow, type TraceCall } from "../lib/api";
import { formatDateTime, formatDuration, judgeVersionCount, statusTone, toTimeline } from "../lib/trace";
import { Badge, Button, Card, EmptyState } from "../components/ui";

// 上游 ID 与展示名相同时不重复拼接（与 Pipelines 页同一规则）
const modelOptionLabel = (model: ModelRow) =>
  model.display_name === model.upstream_model_id
    ? model.display_name
    : `${model.display_name}（${model.upstream_model_id}）`;

/** 调用卡片共用的入参/输出折叠块（成员、透传与裁判的各版本同构）。 */
function CallDetails({ call }: { call: TraceCall }) {
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

/** 状态小圆点的配色（tab 上概览各成员成败，沿用 statusTone 的语义）。 */
const statusDot: Record<"success" | "warn" | "danger" | "muted", string> = {
  success: "bg-emerald-500",
  warn: "bg-amber-500",
  danger: "bg-red-500",
  muted: "bg-slate-300",
};

/**
 * 成员调用组：全部成员模型的调用信息合并为一张卡片，tab 切换查看，
 * 内容多时无需长页滚动（单成员时不渲染 tab 栏）。
 */
function MemberCallsCard({ calls }: { calls: TraceCall[] }) {
  const [active, setActive] = useState(0);
  // 刷新后 calls 长度可能变化，越界时回落到最后一个成员
  const index = Math.min(active, calls.length - 1);
  const call = calls[index];
  return (
    <Card
      title={calls.length > 1 ? `成员调用（${calls.length} 个）` : "成员调用"}
      actions={
        <>
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

/** 两段式裁判第一次调用（评论）的展示状态。 */
interface JudgeCritique {
  status: "streaming" | "success" | "failed" | "cancelled";
  content: string;
  error: string;
  payload: TraceCall["request_payload"] | null;
}

/** 一个裁判版本的展示状态：已落库的行、或进行中/刚结束的流式重跑（content=第二次调用的最终答案）。 */
interface JudgeVersion {
  status: "streaming" | "success" | "failed" | "cancelled";
  content: string;
  error: string;
  payload: TraceCall["request_payload"] | null;
  critique: JudgeCritique | null;
  durationMs: number;
  promptTokens: number;
  completionTokens: number;
  /** 流式期间已接收的增量 chunk 数（OpenAI 兼容流式下 1 chunk ≈ 1 token）。 */
  receivedTokens: number;
}

const critiqueFromCall = (call: TraceCall): JudgeCritique => ({
  status:
    call.status === "success"
      ? "success"
      : call.status === "client_cancelled"
        ? "cancelled"
        : "failed",
  content: call.response_content,
  error: call.error_message,
  payload: call.request_payload,
});

const versionFromCall = (call: TraceCall): JudgeVersion => ({
  status:
    call.status === "success"
      ? "success"
      : call.status === "client_cancelled"
        ? "cancelled"
        : "failed",
  content: call.response_content,
  error: call.error_message,
  payload: call.request_payload,
  critique: null,
  durationMs: call.duration_ms,
  promptTokens: call.prompt_tokens,
  completionTokens: call.completion_tokens,
  receivedTokens: 0,
});

/**
 * 从已落库的裁判行建索引：原始裁判总是入表；重跑仅成功版本入表（失败的视为未生成，可重新发起）。
 * 评论行按"同模型第 k 个评论 ↔ 第 k 个最终行"配对——并行重跑落库交错也能正确对应。
 */
function seedVersions(judges: TraceCall[]): Map<number, JudgeVersion> {
  const critiqueQueues = new Map<number, TraceCall[]>();
  for (const call of judges) {
    if (call.role !== "judge_critique") continue;
    const queue = critiqueQueues.get(call.model_id) ?? [];
    queue.push(call);
    critiqueQueues.set(call.model_id, queue);
  }

  const versions = new Map<number, JudgeVersion>();
  const paired = new Map<number, number>();
  for (const call of judges) {
    if (call.role !== "judge" && call.role !== "judge_rerun") continue;
    const index = paired.get(call.model_id) ?? 0;
    paired.set(call.model_id, index + 1);
    if (call.role === "judge_rerun" && call.status !== "success") continue;
    const version = versionFromCall(call);
    version.critique = critiqueQueues.get(call.model_id)?.[index]
      ? critiqueFromCall(critiqueQueues.get(call.model_id)![index])
      : null;
    versions.set(call.model_id, version);
  }
  return versions;
}

/**
 * 裁判聚合卡：tab 切换裁判模型（原始裁判 + 本次请求的成员模型），第一个 tab 为原始裁判。
 * 无结果的模型展示空态 + "生成"按钮，点击才发起两段式流式重跑（评论 → 最终答案，多模型可并行互不干扰）；
 * 已有结果的模型直接展示，失败/中断的可点"重新生成"。
 */
function JudgeCard({
  traceId,
  judges,
  memberModelIds,
}: {
  traceId: string;
  judges: TraceCall[];
  memberModelIds: number[];
}) {
  const [versions, setVersions] = useState<Map<number, JudgeVersion>>(() => seedVersions(judges));
  // ref 与 state 同步维护：流式回调闭包里需读"最新"表，避免 stale closure 丢增量
  const versionsRef = useRef(versions);
  const originalModelId = judges.find((call) => call.role === "judge")?.model_id ?? judges[0].model_id;
  const [selectedModelId, setSelectedModelId] = useState(originalModelId);
  const { data: models = [] } = useQuery({ queryKey: ["models"], queryFn: api.models.list });

  // 手动刷新详情带来新落库的版本：仅补缺，不打断进行中的流
  useEffect(() => {
    const next = new Map(versionsRef.current);
    let changed = false;
    for (const [id, version] of seedVersions(judges)) {
      if (!next.has(id)) {
        next.set(id, version);
        changed = true;
      }
    }
    if (changed) {
      versionsRef.current = next;
      setVersions(next);
    }
  }, [judges]);

  const setVersion = (id: number, patch: (v: JudgeVersion) => JudgeVersion) => {
    const next = new Map(versionsRef.current);
    const current = next.get(id);
    if (current) {
      next.set(id, patch(current));
      versionsRef.current = next;
      setVersions(next);
    }
  };

  const startStream = (modelId: number) => {
    const next = new Map(versionsRef.current);
    next.set(modelId, {
      status: "streaming",
      content: "",
      error: "",
      payload: null,
      critique: null,
      durationMs: 0,
      promptTokens: 0,
      completionTokens: 0,
      receivedTokens: 0,
    });
    versionsRef.current = next;
    setVersions(next);

    streamRejudge(traceId, modelId, (event) => {
      if (event.type === "meta") {
        if (event.phase === "critique") {
          setVersion(modelId, (v) => ({
            ...v,
            critique: { status: "streaming", content: "", error: "", payload: event.payload },
          }));
        } else {
          setVersion(modelId, (v) => ({ ...v, payload: event.payload }));
        }
      } else if (event.type === "delta") {
        if (event.phase === "critique") {
          setVersion(modelId, (v) => ({
            ...v,
            receivedTokens: v.receivedTokens + 1,
            critique: v.critique
              ? { ...v.critique, content: v.critique.content + event.text }
              : { status: "streaming", content: event.text, error: "", payload: null },
          }));
        } else {
          setVersion(modelId, (v) => ({
            ...v,
            content: v.content + event.text,
            receivedTokens: v.receivedTokens + 1,
          }));
        }
      } else {
        setVersion(modelId, (v) => ({
          ...v,
          status: event.status === "success" ? "success" : "failed",
          content: event.content,
          error: event.error,
          durationMs: event.duration_ms,
          promptTokens: event.prompt_tokens,
          completionTokens: event.completion_tokens,
          critique: v.critique
            ? {
                ...v.critique,
                status: event.critique ? "success" : "failed",
                content: event.critique || v.critique.content,
                error: event.critique ? "" : event.error,
              }
            : null,
        }));
      }
    })
      .then((sawDone) => {
        if (!sawDone) {
          setVersion(modelId, (v) => (v.status === "streaming" ? { ...v, status: "cancelled" } : v));
        }
      })
      .catch((err: unknown) => {
        setVersion(modelId, (v) => (v.status === "streaming" ? { ...v, status: "failed", error: String(err) } : v));
      });
  };

  const selectModel = (modelId: number) => {
    // tab 切换只看不触发：无结果的模型展示空态，由"生成"按钮显式发起
    setSelectedModelId(modelId);
  };

  // tab 范围限定：原始裁判 + 本次请求的成员模型（+历史上已成功重跑过的模型），避免全量模型列表过长
  const optionIds = useMemo(() => {
    const ids: number[] = [originalModelId];
    for (const id of memberModelIds) {
      if (!ids.includes(id)) ids.push(id);
    }
    for (const call of judges) {
      if (call.role === "judge_rerun" && call.status === "success" && !ids.includes(call.model_id)) {
        ids.push(call.model_id);
      }
    }
    return ids;
  }, [originalModelId, memberModelIds, judges]);

  const modelLabel = (id: number) => {
    const model = models.find((m) => m.id === id);
    return model ? modelOptionLabel(model) : `模型 #${id}`;
  };

  const selected = versions.get(selectedModelId);
  // 标题按"已有答案的版本"计数：失败的尝试不占版本号
  const answerCount = Array.from(versions.values()).filter((v) => v.status === "success").length;
  const badgeText = !selected
    ? null
    : selected.status === "streaming"
      ? `生成中（已接收 ${selected.receivedTokens} tokens）`
      : selected.status;
  const badgeTone = !selected
    ? "muted"
    : selected.status === "streaming"
      ? "warn"
      : statusTone(selected.status);
  const critique = selected?.critique ?? null;
  const streamingCritique = selected?.status === "streaming" && critique?.status === "streaming";
  const tabTone = (id: number): keyof typeof statusDot => {
    const v = versions.get(id);
    if (!v) return "muted";
    return v.status === "streaming" ? "warn" : statusTone(v.status);
  };

  return (
    <Card
      title={answerCount > 1 ? `裁判聚合（${answerCount} 个版本）` : "裁判调用"}
      actions={
        selected ? (
          <>
            <Badge tone={badgeTone}>{badgeText}</Badge>
            {selected.status === "streaming" ? null : (
              <span className="text-xs text-slate-400">
                {formatDuration(selected.durationMs)} · {selected.promptTokens}/{selected.completionTokens} tokens
              </span>
            )}
          </>
        ) : undefined
      }
    >
      <div role="tablist" aria-label="裁判模型" className="mb-3 flex flex-wrap gap-x-4 gap-y-1 border-b border-slate-200">
        {optionIds.map((id) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={id === selectedModelId}
            onClick={() => selectModel(id)}
            className={`-mb-px inline-flex max-w-72 items-center gap-1.5 border-b-2 pb-1.5 text-xs transition-colors ${
              id === selectedModelId
                ? "border-slate-900 font-medium text-slate-900"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            <span aria-hidden className={`size-1.5 shrink-0 rounded-full ${statusDot[tabTone(id)]}`} />
            <span className="truncate">{modelLabel(id)}</span>
          </button>
        ))}
      </div>
      {!selected ? (
        <div className="flex flex-col items-center gap-3 py-10 text-sm text-slate-400">
          <p>该模型尚未生成裁判结果</p>
          <Button onClick={() => startStream(selectedModelId)}>生成</Button>
        </div>
      ) : (
        <div className="space-y-2">
          {critique ? (
            <details open={critique.status === "streaming"}>
              <summary className="cursor-pointer text-xs text-slate-500">
                第一次调用 · 评论{critique.status === "streaming" ? "（生成中…）" : ""}
              </summary>
              <div className="mt-1 space-y-1">
                <details>
                  <summary className="cursor-pointer text-xs text-slate-500">入参（实际发出的完整请求）</summary>
                  <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs">
                    {critique.payload ? JSON.stringify(critique.payload, null, 2) : "等待生成…"}
                  </pre>
                </details>
                {/* 评论失败时错误统一在主输出区展示，这里不重复渲染 */}
                {critique.status === "failed" ? null : (
                  <p className="whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-600">
                    {critique.content || "等待评论…"}
                  </p>
                )}
              </div>
            </details>
          ) : null}
          <details>
            <summary className="cursor-pointer text-xs text-slate-500">入参（第二次调用 · 实际发出的完整请求）</summary>
            <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs">
              {selected.payload ? JSON.stringify(selected.payload, null, 2) : "等待生成…"}
            </pre>
          </details>
          {selected.status === "failed" ? (
            <p className="rounded bg-red-50 p-2 text-sm text-red-600">{selected.error || "调用失败"}</p>
          ) : (
            <p className="whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
              {selected.content ||
                (selected.status === "streaming"
                  ? streamingCritique
                    ? "正在生成评论（第一次调用）…"
                    : "等待输出…"
                  : "（空）")}
            </p>
          )}
          {selected.status === "failed" || selected.status === "cancelled" ? (
            <div>
              <Button variant="secondary" onClick={() => startStream(selectedModelId)}>
                重新生成
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </Card>
  );
}

export default function TraceDetail() {
  const { id } = useParams();
  const { data, isPending, isError, error, refetch } = useQuery({
    queryKey: ["trace", id],
    queryFn: () => api.traces.get(id!),
    enabled: Boolean(id),
  });

  if (isPending) return <EmptyState>加载中…</EmptyState>;
  if (isError) return <EmptyState>加载失败：{String(error)}</EmptyState>;
  if (!data) return null;

  const timeline = toTimeline(data);
  const request = data.request;
  const memberModelIds = Array.from(
    new Set(data.calls.filter((call) => call.role === "member").map((call) => call.model_id)),
  );
  const memberCalls = data.calls.filter((call) => call.role === "member");
  // 成员组合成一张 tab 卡片，放在首个成员条目处（原始请求 → 裁判 → 成员 → 最终响应）
  const memberEntryIndex = timeline.findIndex(
    (entry) => entry.kind === "call" && entry.call.role === "member",
  );

  const anchorOf = (index: number) => `trace-entry-${index}`;
  const navItems = timeline.flatMap((entry, index) => {
    if (entry.kind === "request") return [{ anchor: anchorOf(index), label: "原始请求" }];
    if (entry.kind === "final") return [{ anchor: anchorOf(index), label: "最终响应" }];
    if (entry.kind === "judge") {
      const count = judgeVersionCount(entry.calls);
      const first = entry.calls.find((call) => call.role !== "judge_critique");
      return [
        {
          anchor: anchorOf(index),
          label: count > 1 ? `裁判 · ${count} 版` : `裁判 · ${first?.upstream_model_id ?? ""}`,
        },
      ];
    }
    if (entry.call.role === "member") {
      return index === memberEntryIndex
        ? [{ anchor: anchorOf(index), label: `成员 · ${memberCalls.length} 个` }]
        : [];
    }
    return [{ anchor: anchorOf(index), label: `透传 · ${entry.call.upstream_model_id}` }];
  });

  const scrollTo = (anchor: string) =>
    document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-bold">Trace #{request.id}</h1>
        <Badge tone={statusTone(request.status)}>{request.status}</Badge>
        <span className="text-sm text-slate-400">
          {formatDateTime(request.created_at)} · 总耗时 {formatDuration(request.total_duration_ms)} · tokens{" "}
          {request.total_prompt_tokens}/{request.total_completion_tokens}
        </span>
        <Button variant="secondary" onClick={() => refetch()}>
          刷新
        </Button>
      </div>

      {/* 展开长内容时页面仍可能变长，吸顶锚点直达各区块；成员已并为一张 tab 卡，只占一个导航项 */}
      <div className="sticky top-0 z-10 flex flex-wrap items-center gap-1.5 border-b border-slate-200 bg-slate-50/95 py-2 text-xs backdrop-blur">
        <span className="text-slate-400">快速定位</span>
        {navItems.map((item) => (
          <button
            key={item.anchor}
            type="button"
            title={item.label}
            onClick={() => scrollTo(item.anchor)}
            className="max-w-56 truncate rounded-full border border-slate-200 bg-white px-2.5 py-1 text-slate-600 transition-colors hover:border-slate-400 hover:text-slate-900"
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="space-y-3">
        {timeline.map((entry, index) => {
          if (entry.kind === "request") {
            return (
              <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
                <Card title="① 原始请求">
                  <div className="grid grid-cols-3 gap-2 text-sm">
                    <p>
                      <span className="text-slate-400">model 字段：</span>
                      <span className="font-mono">{request.client_model_field}</span>
                    </p>
                    <p>
                      <span className="text-slate-400">Pipeline：</span>
                      {request.pipeline_name || "（透传）"}
                    </p>
                    <p>
                      <span className="text-slate-400">参数：</span>
                      <span className="font-mono text-xs">{JSON.stringify(request.request_params)}</span>
                    </p>
                  </div>
                  <pre className="mt-2 max-h-56 overflow-auto rounded bg-slate-50 p-2 text-xs">
                    {JSON.stringify(request.request_messages, null, 2)}
                  </pre>
                </Card>
              </div>
            );
          }
          if (entry.kind === "final") {
            return (
              <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
                <Card title={`Ⓝ 最终响应（${request.status}）`}>
                  <p className="whitespace-pre-wrap text-sm">{request.response_content || "（空）"}</p>
                </Card>
              </div>
            );
          }
          if (entry.kind === "judge") {
            return (
              <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
                <JudgeCard traceId={id!} judges={entry.calls} memberModelIds={memberModelIds} />
              </div>
            );
          }
          const call = entry.call;
          if (call.role === "member") {
            if (index !== memberEntryIndex) return null;
            return (
              <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
                <MemberCallsCard calls={memberCalls} />
              </div>
            );
          }
          return (
            <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
              <Card
                title="上游调用（透传）"
                actions={
                  <>
                    <span className="font-mono text-xs text-slate-500">{call.upstream_model_id}</span>
                    <Badge tone={statusTone(call.status)}>{call.status}</Badge>
                    <span className="text-xs text-slate-400">
                      {formatDuration(call.duration_ms)} · {call.prompt_tokens}/{call.completion_tokens} tokens
                    </span>
                  </>
                }
              >
                <CallDetails call={call} />
              </Card>
            </div>
          );
        })}
      </div>
    </div>
  );
}
