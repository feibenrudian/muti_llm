import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, streamRejudge, type ModelRow, type TraceCall } from "../lib/api";
import { formatDateTime, formatDuration, statusTone, toTimeline } from "../lib/trace";
import { Badge, Button, Card, EmptyState, Select } from "../components/ui";

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

/** 一个裁判版本的展示状态：已落库的行、或进行中/刚结束的流式重跑。 */
interface JudgeVersion {
  status: "streaming" | "success" | "failed" | "cancelled";
  content: string;
  error: string;
  payload: TraceCall["request_payload"] | null;
  durationMs: number;
  promptTokens: number;
  completionTokens: number;
}

const versionFromCall = (call: TraceCall): JudgeVersion => ({
  status: call.status === "success" ? "success" : call.status === "client_cancelled" ? "cancelled" : "failed",
  content: call.response_content,
  error: call.error_message,
  payload: call.request_payload,
  durationMs: call.duration_ms,
  promptTokens: call.prompt_tokens,
  completionTokens: call.completion_tokens,
});

/** 从已落库的裁判行建索引：原始裁判总是入表；重跑仅成功版本入表（失败的视为未生成，可重新发起）。 */
function seedVersions(judges: TraceCall[]): Map<number, JudgeVersion> {
  const versions = new Map<number, JudgeVersion>();
  for (const call of judges) {
    if (call.role === "judge" || call.status === "success") {
      versions.set(call.model_id, versionFromCall(call));
    }
  }
  return versions;
}

/**
 * 裁判聚合卡：下拉选择裁判模型（原始裁判 + 本次请求的成员模型）。
 * 选中无结果的模型立即发起流式重跑（多模型可并行，互不干扰）；选中已有结果的模型直接展示。
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
  const originalModelId = judges[0].model_id;
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
      durationMs: 0,
      promptTokens: 0,
      completionTokens: 0,
    });
    versionsRef.current = next;
    setVersions(next);

    streamRejudge(traceId, modelId, (event) => {
      if (event.type === "meta") {
        setVersion(modelId, (v) => ({ ...v, payload: event.payload }));
      } else if (event.type === "delta") {
        setVersion(modelId, (v) => ({ ...v, content: v.content + event.text }));
      } else {
        setVersion(modelId, (v) => ({
          ...v,
          status: event.status === "success" ? "success" : "failed",
          content: event.content,
          error: event.error,
          durationMs: event.duration_ms,
          promptTokens: event.prompt_tokens,
          completionTokens: event.completion_tokens,
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
    setSelectedModelId(modelId);
    const existing = versionsRef.current.get(modelId);
    // 已有成功结果或正在生成：直接展示；失败/中断的尝试不算结果，重新发起
    if (!existing || (existing.status !== "success" && existing.status !== "streaming")) {
      startStream(modelId);
    }
  };

  // 下拉范围限定：原始裁判 + 本次请求的成员模型（+历史上已成功重跑过的模型），避免全量模型列表过长
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
  if (!selected) return null;
  // 标题按"已有答案的版本"计数：失败的尝试不占版本号
  const answerCount = Array.from(versions.values()).filter((v) => v.status === "success").length;
  const badgeText = selected.status === "streaming" ? "生成中" : selected.status;
  const badgeTone = selected.status === "streaming" ? "warn" : statusTone(selected.status);

  return (
    <Card
      title={answerCount > 1 ? `裁判聚合（${answerCount} 个版本）` : "裁判调用"}
      actions={
        <>
          <div className="w-64">
            <Select aria-label="裁判模型" value={String(selectedModelId)} onChange={(e) => selectModel(Number(e.target.value))}>
              {optionIds.map((id) => (
                <option key={id} value={id}>
                  {modelLabel(id)}
                </option>
              ))}
            </Select>
          </div>
          <Badge tone={badgeTone}>{badgeText}</Badge>
          {selected.status === "streaming" ? null : (
            <span className="text-xs text-slate-400">
              {formatDuration(selected.durationMs)} · {selected.promptTokens}/{selected.completionTokens} tokens
            </span>
          )}
        </>
      }
    >
      <details>
        <summary className="cursor-pointer text-xs text-slate-500">入参（实际发出的完整请求）</summary>
        <pre className="mt-1 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-xs">
          {selected.payload ? JSON.stringify(selected.payload, null, 2) : "等待生成…"}
        </pre>
      </details>
      {selected.status === "failed" ? (
        <p className="mt-2 rounded bg-red-50 p-2 text-sm text-red-600">{selected.error || "调用失败"}</p>
      ) : (
        <p className="mt-2 whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
          {selected.content || (selected.status === "streaming" ? "等待输出…" : "（空）")}
        </p>
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

  // 成员按时间线顺序编号，卡片标题与顶部快速定位一一对应
  let memberCount = 0;
  const memberNo = new Map<number, number>();
  for (const [index, entry] of timeline.entries()) {
    if (entry.kind === "call" && entry.call.role === "member") memberNo.set(index, (memberCount += 1));
  }

  const anchorOf = (index: number) => `trace-entry-${index}`;
  const navItems = timeline.map((entry, index) => {
    if (entry.kind === "request") return { anchor: anchorOf(index), label: "原始请求" };
    if (entry.kind === "final") return { anchor: anchorOf(index), label: "最终响应" };
    if (entry.kind === "judge") {
      return {
        anchor: anchorOf(index),
        label:
          entry.calls.length > 1
            ? `裁判 · ${entry.calls.length} 版`
            : `裁判 · ${entry.calls[0].upstream_model_id}`,
      };
    }
    const model = entry.call.upstream_model_id;
    const label =
      entry.call.role === "passthrough"
        ? `透传 · ${model}`
        : `成员${memberNo.get(index)} · ${model}`;
    return { anchor: anchorOf(index), label };
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

      {/* 模型多时页面很长，吸顶锚点直达目标调用，入参默认折叠保持紧凑 */}
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
          const label = call.role === "passthrough" ? "上游调用（透传）" : `成员调用 ${memberNo.get(index)}`;
          return (
            <div key={`entry-${index}`} id={anchorOf(index)} className="scroll-mt-12">
              <Card
                title={label}
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
