import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDateTime, formatDuration, statusTone, toTimeline } from "../lib/trace";
import { Badge, Button, Card, EmptyState } from "../components/ui";

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

  // 成员按时间线顺序编号，卡片标题与顶部快速定位一一对应
  let memberCount = 0;
  const memberNo = new Map<number, number>();
  for (const [index, entry] of timeline.entries()) {
    if (entry.kind === "call" && entry.call?.role === "member") memberNo.set(index, (memberCount += 1));
  }

  const anchorOf = (index: number) => `trace-entry-${index}`;
  const navItems = timeline.map((entry, index) => {
    if (entry.kind === "request") return { anchor: anchorOf(index), label: "原始请求" };
    if (entry.kind === "final") return { anchor: anchorOf(index), label: "最终响应" };
    const call = entry.call!;
    const model = call.upstream_model_id;
    const label =
      call.role === "judge"
        ? `裁判 · ${model}`
        : call.role === "passthrough"
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
          const call = entry.call!;
          const label =
            call.role === "judge"
              ? "裁判调用"
              : call.role === "passthrough"
                ? "上游调用（透传）"
                : `成员调用 ${memberNo.get(index)}`;
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
              </Card>
            </div>
          );
        })}
      </div>
    </div>
  );
}
