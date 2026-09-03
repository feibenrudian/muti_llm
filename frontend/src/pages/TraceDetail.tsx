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

      <div className="space-y-3">
        {timeline.map((entry, index) => {
          if (entry.kind === "request") {
            return (
              <Card key={`entry-${index}`} title="① 原始请求">
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
            );
          }
          if (entry.kind === "final") {
            return (
              <Card key={`entry-${index}`} title={`Ⓝ 最终响应（${request.status}）`}>
                <p className="whitespace-pre-wrap text-sm">{request.response_content || "（空）"}</p>
              </Card>
            );
          }
          const call = entry.call!;
          const label =
            call.role === "judge" ? "裁判调用" : call.role === "passthrough" ? "上游调用（透传）" : "成员调用";
          return (
            <Card
              key={`entry-${index}`}
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
                <details open>
                  <summary className="mt-2 cursor-pointer text-xs text-slate-500">输出</summary>
                  <p className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
                    {call.response_content || "（空）"}
                  </p>
                </details>
              ) : (
                <p className="mt-2 rounded bg-red-50 p-2 text-sm text-red-600">{call.error_message}</p>
              )}
            </Card>
          );
        })}
      </div>
    </div>
  );
}
