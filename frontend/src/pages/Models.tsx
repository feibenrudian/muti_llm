import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ModelRow, type Provider } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, Modal, Select, Td, Th } from "../components/ui";

interface FormState {
  provider_id: string;
  display_name: string;
  upstream_model_id: string;
  temperature: string;
  max_tokens: string;
  timeout_seconds: string;
}

const EMPTY: FormState = { provider_id: "", display_name: "", upstream_model_id: "", temperature: "", max_tokens: "", timeout_seconds: "" };

type TestResult = { ok: boolean; latency_ms: number; content?: string; error?: string };

export default function Models() {
  const queryClient = useQueryClient();
  const { data: models = [], isPending } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const { data: providers = [] } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<Record<number, TestResult | "loading">>({});

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["models"] });
  const providerName = (id: number) => providers.find((p: Provider) => p.id === id)?.name ?? `#${id}`;

  const create = useMutation({
    mutationFn: async () => {
      const default_params: Record<string, number> = {};
      if (form.temperature) default_params.temperature = Number(form.temperature);
      if (form.max_tokens) default_params.max_tokens = Number(form.max_tokens);
      if (form.timeout_seconds) default_params.timeout_seconds = Number(form.timeout_seconds);
      if (Number(form.temperature) < 0 || Number(form.temperature) > 2) {
        throw new Error("temperature 需在 0–2 之间");
      }
      return api.models.create({
        provider_id: Number(form.provider_id),
        display_name: form.display_name,
        upstream_model_id: form.upstream_model_id,
        default_params,
      });
    },
    onSuccess: () => {
      setCreating(false);
      setForm(EMPTY);
      setError("");
      void invalidate();
    },
    onError: (err) => setError(err instanceof ApiError || err instanceof Error ? err.message : String(err)),
  });

  const remove = useMutation({ mutationFn: (id: number) => api.models.remove(id), onSuccess: invalidate });
  const test = useMutation({
    mutationFn: (id: number) => api.models.test(id),
    onMutate: (id) => setTestResult((prev) => ({ ...prev, [id]: "loading" })),
    onSuccess: (result, id) => setTestResult((prev) => ({ ...prev, [id]: result })),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">模型（Model）</h1>
      <Card title={`共 ${models.length} 个`} actions={<Button onClick={() => setCreating(true)}>新建模型</Button>}>
        {isPending ? (
          <EmptyState>加载中…</EmptyState>
        ) : models.length === 0 ? (
          <EmptyState>暂无模型</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>名称</Th>
                <Th>供应商</Th>
                <Th>上游模型 ID</Th>
                <Th>默认参数</Th>
                <Th>连通性</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {models.map((model: ModelRow) => {
                const result = testResult[model.id];
                return (
                  <tr key={model.id} className="border-b border-slate-50">
                    <Td>{model.display_name}</Td>
                    <Td>{providerName(model.provider_id)}</Td>
                    <Td className="font-mono text-xs">{model.upstream_model_id}</Td>
                    <Td className="font-mono text-xs">{JSON.stringify(model.default_params)}</Td>
                    <Td>
                      {result === undefined ? null : result === "loading" ? (
                        <Badge tone="muted">测试中…</Badge>
                      ) : result.ok ? (
                        <Badge tone="success">成功 {result.latency_ms}ms</Badge>
                      ) : (
                        <Badge tone="danger" >失败</Badge>
                      )}
                      {result && result !== "loading" && !result.ok ? (
                        <p className="mt-1 max-w-64 text-xs text-red-600">{result.error}</p>
                      ) : null}
                      {result && result !== "loading" && result.ok && result.content ? (
                        <p className="mt-1 text-xs text-slate-400">回复: {result.content}</p>
                      ) : null}
                    </Td>
                    <Td>
                      <div className="flex gap-1">
                        <Button variant="ghost" onClick={() => test.mutate(model.id)}>
                          测试
                        </Button>
                        <Button variant="danger" onClick={() => remove.mutate(model.id)}>
                          删除
                        </Button>
                      </div>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>
      <div className="max-w-2xl">
        <ErrorText>{error}</ErrorText>
      </div>

      <Modal open={creating} title="新建模型" onClose={() => setCreating(false)}>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <Field label="所属供应商">
            <Select value={form.provider_id} onChange={(e) => setForm({ ...form, provider_id: e.target.value })} required>
              <option value="">请选择…</option>
              {providers.map((provider: Provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="显示名称">
            <Input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} required />
          </Field>
          <Field label="上游模型 ID">
            <Input
              value={form.upstream_model_id}
              onChange={(e) => setForm({ ...form, upstream_model_id: e.target.value })}
              placeholder="deepseek-v4-flash"
              required
            />
          </Field>
          <div className="grid grid-cols-3 gap-3">
            <Field label="temperature" hint="0–2">
              <Input value={form.temperature} onChange={(e) => setForm({ ...form, temperature: e.target.value })} placeholder="0.7" />
            </Field>
            <Field label="max_tokens">
              <Input value={form.max_tokens} onChange={(e) => setForm({ ...form, max_tokens: e.target.value })} placeholder="2048" />
            </Field>
            <Field label="timeout(秒)">
              <Input value={form.timeout_seconds} onChange={(e) => setForm({ ...form, timeout_seconds: e.target.value })} placeholder="60" />
            </Field>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setCreating(false)}>
              取消
            </Button>
            <Button type="submit" disabled={create.isPending}>
              保存
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
