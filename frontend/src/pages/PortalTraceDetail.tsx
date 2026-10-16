/** 门户 Trace 详情（T52）：只读时间线（原始请求 → 成员/裁判 → 最终响应）。
 *
 * 展示组件与 TraceDetail 共用（components/traceView）；裁判流式重跑属管理台功能，
 * 此处裁判各版本按只读轮次块展示。
 */

import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type TraceCall } from "../lib/api";
import { formatDateTime, formatDuration, statusTone, toTimeline } from "../lib/trace";
import { Badge, Card, EmptyState } from "../components/ui";
import { CallDetails, JudgeRound, MemberCallsCard } from "../components/traceView";

/** 只读裁判卡：该 trace 的全部裁判调用（原始裁判 + 历史重跑版本）按轮次折叠展示。 */
function ReadonlyJudgeCard({ judges }: { judges: TraceCall[] }) {
  return (
    <Card title={`裁判调用（${judges.length} 条记录）`}>
      <div className="space-y-2">
        {judges.map((call, i) => (
          <JudgeRound
            key={call.id}
            roundNo={i + 1}
            kind={`${call.role} · ${call.upstream_model_id}`}
            live={false}
            payload={call.request_payload}
            stats={`${formatDuration(call.duration_ms)} · ${call.prompt_tokens}/${call.completion_tokens} tokens`}
          >
            {call.status === "success" ? (
              <p className="whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
                {call.response_content || "（空）"}
              </p>
            ) : (
              <p className="rounded bg-red-50 p-2 text-sm text-red-600">{call.error_message}</p>
            )}
          </JudgeRound>
        ))}
      </div>
    </Card>
  );
}

export default function PortalTraceDetail() {
  const { id } = useParams();
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["portal-trace", id],
    queryFn: () => api.portal.traces.get(id!),
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
        <h1 className="text-lg font-bold">调用记录 #{request.id}</h1>
        <Badge tone={statusTone(request.status)}>{request.status}</Badge>
        <span className="text-sm text-slate-400">
          {formatDateTime(request.created_at)} · 总耗时 {formatDuration(request.total_duration_ms)} ·
          tokens {request.total_prompt_tokens}/{request.total_completion_tokens}
        </span>
      </div>

      {timeline.map((entry, index) => {
        if (entry.kind === "request") {
          return (
            <Card key="request" title="原始请求">
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
            <Card
              key="final"
              title={`最终响应（${request.status}）`}
              actions={
                <span className="text-xs text-slate-400">
                  总耗时 {formatDuration(request.total_duration_ms)}
                </span>
              }
            >
              <p className="whitespace-pre-wrap text-sm">{request.response_content || "（空）"}</p>
            </Card>
          );
        }
        if (entry.kind === "judge") {
          return <ReadonlyJudgeCard key={`judge-${index}`} judges={entry.calls} />;
        }
        if (entry.kind === "member_group") {
          return (
            <MemberCallsCard
              key={`members-${entry.round ?? "all"}-${index}`}
              calls={entry.calls}
              round={entry.round}
            />
          );
        }
        const call = entry.call;
        if (call.role === "member") {
          return null; // 单成员组已在 member_group 合并；防重复渲染
        }
        return (
          <Card
            key={`passthrough-${index}`}
            title="上游调用（透传）"
            actions={
              <>
                <span className="font-mono text-xs text-slate-500">{call.upstream_model_id}</span>
                <Badge tone={statusTone(call.status)}>{call.status}</Badge>
                <span className="text-xs text-slate-400">
                  {formatDuration(call.duration_ms)} · {call.prompt_tokens}/{call.completion_tokens}{" "}
                  tokens
                </span>
              </>
            }
          >
            <CallDetails call={call} />
          </Card>
        );
      })}
    </div>
  );
}
