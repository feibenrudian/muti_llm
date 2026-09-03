import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, type Pipeline, type PlaygroundResult, type TraceDetail } from "../lib/api";
import { formatDuration, statusTone } from "../lib/trace";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Select, Textarea } from "../components/ui";

export default function Playground() {
  const { data: pipelines = [] } = useQuery({ queryKey: ["pipelines"], queryFn: api.pipelines.list });
  const [pipelineName, setPipelineName] = useState("");
  const [message, setMessage] = useState("用一句话解释量子纠缠");
  const [error, setError] = useState("");
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [trace, setTrace] = useState<TraceDetail | null>(null);

  const run = useMutation({
    mutationFn: async () => {
      const name = pipelineName || (pipelines[0] as Pipeline | undefined)?.name;
      if (!name) throw new Error("没有可用的 Pipeline，请先在「组合」页创建");
      setResult(null);
      setTrace(null);
      setError("");
      const payload = await api.playground.run({ pipeline_name: name, message });
      setResult(payload);
      if (payload.trace_id) {
        setTrace(await api.traces.get(payload.trace_id));
      }
      return payload;
    },
    onError: (err) => setError(err instanceof Error ? err.message : String(err)),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">试运行（Playground）</h1>

      <Card title="发起测试">
        <div className="flex items-start gap-3">
          <div className="w-64 shrink-0">
            <Field label="Pipeline（虚拟模型）">
              <Select value={pipelineName} onChange={(e) => setPipelineName(e.target.value)}>
                {pipelines.length === 0 ? <option value="">（无可用 Pipeline）</option> : null}
                {pipelines.map((pipeline: Pipeline) => (
                  <option key={pipeline.id} value={pipeline.name}>
                    {pipeline.name}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <div className="flex-1">
            <Field label="用户消息">
              <Textarea rows={3} value={message} onChange={(e) => setMessage(e.target.value)} />
            </Field>
          </div>
          <Button className="mt-6" onClick={() => run.mutate()} disabled={run.isPending || !message.trim()}>
            {run.isPending ? "运行中…" : "运行"}
          </Button>
        </div>
        <div className="mt-3">
          <ErrorText>{error}</ErrorText>
          {result && result.status_code !== 200 && result.response?.error ? (
            <ErrorText>上游返回 {result.status_code}：{result.response.error.message}</ErrorText>
          ) : null}
        </div>
      </Card>

      {trace ? (
        <Card title={`执行链路（trace #${trace.request.id}）`}>
          <div className="space-y-3">
            {trace.calls.map((call) => (
              <div key={call.id} className="rounded-md border border-slate-200 p-3">
                <div className="mb-2 flex items-center gap-2 text-sm">
                  <Badge tone={call.role === "judge" ? "warn" : "muted"}>
                    {call.role === "judge" ? "裁判" : call.role === "passthrough" ? "透传" : "成员"}
                  </Badge>
                  <span className="font-mono text-xs">{call.upstream_model_id}</span>
                  <Badge tone={statusTone(call.status)}>{call.status}</Badge>
                  <span className="text-xs text-slate-400">
                    {formatDuration(call.duration_ms)} · {call.prompt_tokens}/{call.completion_tokens} tokens
                  </span>
                </div>
                <details>
                  <summary className="cursor-pointer text-xs text-slate-500">入参（实际发出的请求）</summary>
                  <pre className="mt-1 max-h-56 overflow-auto rounded bg-slate-50 p-2 text-xs">
                    {JSON.stringify(call.request_payload, null, 2)}
                  </pre>
                </details>
                {call.status === "success" ? (
                  <details open>
                    <summary className="cursor-pointer text-xs text-slate-500">输出</summary>
                    <p className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 text-sm">
                      {call.response_content}
                    </p>
                  </details>
                ) : (
                  <p className="mt-1 text-sm text-red-600">{call.error_message}</p>
                )}
              </div>
            ))}

            <div className="rounded-md border-2 border-slate-800 p-3">
              <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
                最终答案（{trace.request.status}）
                {result?.response?.degraded ? <Badge tone="warn">degraded 降级</Badge> : null}
              </div>
              <p className="whitespace-pre-wrap text-sm">{trace.request.response_content}</p>
            </div>
          </div>
        </Card>
      ) : !run.isPending && !error ? (
        <EmptyState>运行一次试试——将展示各成员答案、裁判 Prompt 与最终答案的完整链路</EmptyState>
      ) : null}
    </div>
  );
}
