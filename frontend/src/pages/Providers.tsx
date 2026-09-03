import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, detectProtocol, type Provider, type ProviderTestResult } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, Modal, Td, Th } from "../components/ui";

interface FormState {
  name: string;
  base_url: string;
  api_key: string;
  remark: string;
}

const EMPTY: FormState = { name: "", base_url: "", api_key: "", remark: "" };

export default function Providers() {
  const queryClient = useQueryClient();
  const { data: providers = [], isPending } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const [editing, setEditing] = useState<Provider | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState<Record<number, ProviderTestResult | "loading">>({});

  // 创建/测试会自动同步模型、删除会级联清理 → providers 与 models 两个列表一并失效
  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
    void queryClient.invalidateQueries({ queryKey: ["models"] });
  };
  const save = useMutation({
    mutationFn: async () => {
      const body = { ...form, protocol: detectProtocol(form.base_url) };
      if (editing && !body.api_key) delete (body as Record<string, unknown>).api_key;
      return editing ? api.providers.update(editing.id, body) : api.providers.create(body);
    },
    onSuccess: () => {
      setCreating(false);
      setEditing(null);
      setForm(EMPTY);
      setError("");
      void invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const toggle = useMutation({
    mutationFn: (provider: Provider) => api.providers.update(provider.id, { enabled: !provider.enabled }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.providers.remove(id),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const test = useMutation({
    mutationFn: (id: number) => api.providers.test(id),
    onMutate: (id) => setTestResult((prev) => ({ ...prev, [id]: "loading" })),
    onSuccess: (result, id) => {
      setTestResult((prev) => ({ ...prev, [id]: result }));
      if (result.synced && result.synced.length > 0) invalidate();
    },
    onError: (err, id) =>
      setTestResult((prev) => ({
        ...prev,
        [id]: { ok: false, latency_ms: 0, error: err instanceof ApiError ? err.message : String(err) },
      })),
  });

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY);
    setError("");
    setCreating(true);
  };
  const openEdit = (provider: Provider) => {
    setCreating(false);
    setEditing(provider);
    setForm({
      name: provider.name,
      base_url: provider.base_url,
      api_key: "",
      remark: provider.remark,
    });
    setError("");
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">供应商（Provider）</h1>
      <Card
        title={`共 ${providers.length} 个`}
        actions={<Button onClick={openCreate}>新建 Provider</Button>}
      >
        {isPending ? (
          <EmptyState>加载中…</EmptyState>
        ) : providers.length === 0 ? (
          <EmptyState>暂无供应商，点击右上角新建</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>名称</Th>
                <Th>协议</Th>
                <Th>Base URL</Th>
                <Th>API Key</Th>
                <Th>连通性</Th>
                <Th>状态</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {providers.map((provider) => {
                const result = testResult[provider.id];
                return (
                  <tr key={provider.id} className="border-b border-slate-50">
                    <Td>{provider.name}</Td>
                    <Td>{provider.protocol}</Td>
                    <Td className="max-w-64 truncate">{provider.base_url}</Td>
                    <Td>{provider.api_key_masked || "—"}</Td>
                    <Td>
                      {result === undefined ? null : result === "loading" ? (
                        <Badge tone="muted">测试中…</Badge>
                      ) : result.ok ? (
                        <Badge tone="success">成功 {result.latency_ms}ms</Badge>
                      ) : (
                        <Badge tone="danger">失败</Badge>
                      )}
                      {result && result !== "loading" && !result.ok ? (
                        <p className="mt-1 max-w-64 text-xs text-red-600">{result.error}</p>
                      ) : null}
                      {result && result !== "loading" && result.ok ? (
                        <>
                          <p className="mt-1 max-w-64 truncate text-xs text-slate-400">
                            可用模型: {(result.models ?? []).join("、") || "（上游未返回模型列表）"}
                          </p>
                          {result.synced && result.synced.length > 0 ? (
                            <p className="text-xs text-emerald-600">
                              同步新增 {result.synced.length} 个模型
                            </p>
                          ) : null}
                        </>
                      ) : null}
                    </Td>
                    <Td>
                      <Badge tone={provider.enabled ? "success" : "muted"}>
                        {provider.enabled ? "启用" : "停用"}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex gap-1">
                        <Button variant="ghost" onClick={() => test.mutate(provider.id)}>
                          测试
                        </Button>
                        <Button variant="ghost" onClick={() => openEdit(provider)}>
                          编辑
                        </Button>
                        <Button variant="ghost" onClick={() => toggle.mutate(provider)}>
                          {provider.enabled ? "停用" : "启用"}
                        </Button>
                        <Button variant="danger" onClick={() => remove.mutate(provider.id)}>
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

      <Modal
        open={creating || editing !== null}
        title={editing ? `编辑 ${editing.name}` : "新建 Provider"}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      >
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <Field label="名称">
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          </Field>
          <Field
            label="Base URL"
            hint={
              form.base_url
                ? `已识别协议：${detectProtocol(form.base_url) === "anthropic" ? "anthropic" : "openai_compatible（OpenAI 兼容）"}`
                : "OpenAI 兼容服务填到 /v1（如 https://api.deepseek.com）；Anthropic 官方填 https://api.anthropic.com。协议按 URL 自动识别，无需手选"
            }
          >
            <Input
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
              placeholder="https://api.deepseek.com"
              required
            />
          </Field>
          <Field label="API Key" hint={editing ? "留空则保持不变；保存后仅显示尾 4 位" : "加密存储，仅显示尾 4 位"}>
            <Input
              type="password"
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              placeholder={editing ? "（不修改则留空）" : "sk-…"}
            />
          </Field>
          <Field label="备注">
            <Input value={form.remark} onChange={(e) => setForm({ ...form, remark: e.target.value })} />
          </Field>
          <ErrorText>{save.isError ? error : ""}</ErrorText>
          <div className="flex justify-end gap-2 pt-2">
            <Button
              variant="secondary"
              onClick={() => {
                setCreating(false);
                setEditing(null);
              }}
            >
              取消
            </Button>
            <Button type="submit" disabled={save.isPending}>
              保存
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
