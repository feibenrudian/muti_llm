import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ModelRow, type Pipeline } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, Modal, Select, Td, Textarea, Th } from "../components/ui";

interface MemberDraft {
  model_id: string;
  temperature: string;
}

interface FormState {
  name: string;
  judge_model_id: string;
  judge_prompt_template: string;
  members: MemberDraft[];
}

export default function Pipelines() {
  const queryClient = useQueryClient();
  const { data: pipelines = [], isPending } = useQuery({ queryKey: ["pipelines"], queryFn: api.pipelines.list });
  const { data: models = [] } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const [editing, setEditing] = useState<Pipeline | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>({ name: "", judge_model_id: "", judge_prompt_template: "", members: [{ model_id: "", temperature: "" }] });
  const [error, setError] = useState("");
  const [defaultTemplate, setDefaultTemplate] = useState("");

  useEffect(() => {
    api.meta.judgeTemplate().then((data) => setDefaultTemplate(data.template)).catch(() => undefined);
  }, []);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["pipelines"] });
  const modelName = (id: number) => models.find((m: ModelRow) => m.id === id)?.display_name ?? `#${id}`;

  const buildBody = () => ({
    name: form.name,
    strategy: "council",
    judge_model_id: Number(form.judge_model_id),
    judge_prompt_template: form.judge_prompt_template,
    members: form.members
      .filter((member) => member.model_id)
      .map((member) => ({
        model_id: Number(member.model_id),
        ...(member.temperature ? { param_overrides: { temperature: Number(member.temperature) } } : {}),
      })),
  });

  const save = useMutation({
    mutationFn: async () => {
      if (!form.judge_model_id) throw new Error("必须选择裁判模型");
      if (form.members.filter((member) => member.model_id).length === 0) throw new Error("至少选择一个成员模型");
      const body = buildBody();
      return editing ? api.pipelines.update(editing.id, body) : api.pipelines.create(body);
    },
    onSuccess: () => {
      setCreating(false);
      setEditing(null);
      setError("");
      void invalidate();
    },
    onError: (err) => setError(err instanceof ApiError || err instanceof Error ? err.message : String(err)),
  });
  const remove = useMutation({ mutationFn: (id: number) => api.pipelines.remove(id), onSuccess: invalidate });
  const toggle = useMutation({
    mutationFn: (pipeline: Pipeline) => api.pipelines.update(pipeline.id, { enabled: !pipeline.enabled }),
    onSuccess: invalidate,
  });

  const openCreate = () => {
    setEditing(null);
    setForm({ name: "", judge_model_id: "", judge_prompt_template: defaultTemplate, members: [{ model_id: "", temperature: "" }] });
    setError("");
    setCreating(true);
  };
  const openEdit = (pipeline: Pipeline) => {
    setCreating(false);
    setEditing(pipeline);
    setForm({
      name: pipeline.name,
      judge_model_id: String(pipeline.judge_model_id),
      judge_prompt_template: pipeline.judge_prompt_template || defaultTemplate,
      members: pipeline.members.map((member) => ({
        model_id: String(member.model_id),
        temperature: member.param_overrides.temperature !== undefined ? String(member.param_overrides.temperature) : "",
      })),
    });
    setError("");
  };

  const moveMember = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= form.members.length) return;
    const members = [...form.members];
    [members[index], members[target]] = [members[target], members[index]];
    setForm({ ...form, members });
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">组合（Pipeline / 虚拟模型）</h1>
      <Card title={`共 ${pipelines.length} 个`} actions={<Button onClick={openCreate}>新建 Pipeline</Button>}>
        {isPending ? (
          <EmptyState>加载中…</EmptyState>
        ) : pipelines.length === 0 ? (
          <EmptyState>暂无 Pipeline</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>名称（虚拟模型）</Th>
                <Th>策略</Th>
                <Th>成员</Th>
                <Th>裁判</Th>
                <Th>状态</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {pipelines.map((pipeline: Pipeline) => (
                <tr key={pipeline.id} className="border-b border-slate-50">
                  <Td className="font-mono">{pipeline.name}</Td>
                  <Td>{pipeline.strategy}</Td>
                  <Td>{pipeline.members.map((m) => modelName(m.model_id)).join("、")}</Td>
                  <Td>{modelName(pipeline.judge_model_id)}</Td>
                  <Td>
                    <Badge tone={pipeline.enabled ? "success" : "muted"}>{pipeline.enabled ? "启用" : "停用"}</Badge>
                  </Td>
                  <Td>
                    <div className="flex gap-1">
                      <Button variant="ghost" onClick={() => openEdit(pipeline)}>
                        编辑
                      </Button>
                      <Button variant="ghost" onClick={() => toggle.mutate(pipeline)}>
                        {pipeline.enabled ? "停用" : "启用"}
                      </Button>
                      <Button variant="danger" onClick={() => remove.mutate(pipeline.id)}>
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
        title={editing ? `编辑 ${editing.name}` : "新建 Pipeline"}
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
          <div className="grid grid-cols-2 gap-3">
            <Field label="名称（对外虚拟模型名）" hint="小写字母/数字/-/_">
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="council-v1" required />
            </Field>
            <Field label="裁判模型" hint="必选">
              <Select value={form.judge_model_id} onChange={(e) => setForm({ ...form, judge_model_id: e.target.value })}>
                <option value="">请选择…</option>
                {models.map((model: ModelRow) => (
                  <option key={model.id} value={model.id}>
                    {model.display_name}（{model.upstream_model_id}）
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          <div className="space-y-2">
            <p className="text-sm font-medium text-slate-700">成员模型（按顺序并发调用）</p>
            {form.members.map((member, index) => (
              <div key={index} className="flex items-center gap-2">
                <span className="w-5 text-xs text-slate-400">#{index + 1}</span>
                <Select
                  className="flex-1"
                  value={member.model_id}
                  onChange={(e) => {
                    const members = [...form.members];
                    members[index] = { ...member, model_id: e.target.value };
                    setForm({ ...form, members });
                  }}
                >
                  <option value="">请选择…</option>
                  {models.map((model: ModelRow) => (
                    <option key={model.id} value={model.id}>
                      {model.display_name}（{model.upstream_model_id}）
                    </option>
                  ))}
                </Select>
                <Input
                  className="w-28"
                  placeholder="temp 覆盖"
                  value={member.temperature}
                  onChange={(e) => {
                    const members = [...form.members];
                    members[index] = { ...member, temperature: e.target.value };
                    setForm({ ...form, members });
                  }}
                />
                <Button variant="ghost" onClick={() => moveMember(index, -1)} aria-label={`成员${index + 1}上移`}>
                  ↑
                </Button>
                <Button variant="ghost" onClick={() => moveMember(index, 1)} aria-label={`成员${index + 1}下移`}>
                  ↓
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => setForm({ ...form, members: form.members.filter((_, i) => i !== index) })}
                  aria-label={`移除成员${index + 1}`}
                >
                  ✕
                </Button>
              </div>
            ))}
            <Button variant="secondary" onClick={() => setForm({ ...form, members: [...form.members, { model_id: "", temperature: "" }] })}>
              + 添加成员
            </Button>
          </div>

          <Field label="裁判 Prompt 模板" hint="占位符：{{original_messages}} / {{candidate_answers}}，留空使用默认模板">
            <Textarea
              rows={6}
              value={form.judge_prompt_template}
              onChange={(e) => setForm({ ...form, judge_prompt_template: e.target.value })}
            />
          </Field>
          <Button variant="secondary" onClick={() => setForm({ ...form, judge_prompt_template: defaultTemplate })}>
            恢复默认模板
          </Button>

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
