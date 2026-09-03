import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type Provider } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, Modal, Select, Td, Th } from "../components/ui";

interface FormState {
  name: string;
  protocol: string;
  base_url: string;
  api_key: string;
  remark: string;
}

const EMPTY: FormState = { name: "", protocol: "openai_compatible", base_url: "", api_key: "", remark: "" };

export default function Providers() {
  const queryClient = useQueryClient();
  const { data: providers = [], isPending } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const [editing, setEditing] = useState<Provider | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [error, setError] = useState("");

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["providers"] });
  const save = useMutation({
    mutationFn: async () => {
      const body = { ...form };
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
      protocol: provider.protocol,
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
                <Th>状态</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {providers.map((provider) => (
                <tr key={provider.id} className="border-b border-slate-50">
                  <Td>{provider.name}</Td>
                  <Td>{provider.protocol}</Td>
                  <Td className="max-w-64 truncate">{provider.base_url}</Td>
                  <Td>{provider.api_key_masked || "—"}</Td>
                  <Td>
                    <Badge tone={provider.enabled ? "success" : "muted"}>
                      {provider.enabled ? "启用" : "停用"}
                    </Badge>
                  </Td>
                  <Td>
                    <div className="flex gap-1">
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
              ))}
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
          <Field label="协议">
            <Select value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value })}>
              <option value="openai_compatible">openai_compatible（OpenAI/DeepSeek/Ollama 等）</option>
              <option value="anthropic">anthropic</option>
            </Select>
          </Field>
          <Field label="Base URL" hint="OpenAI 兼容服务填到 /v1，如 https://api.deepseek.com">
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
