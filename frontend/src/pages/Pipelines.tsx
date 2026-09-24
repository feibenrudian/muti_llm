import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ModelRow, type Pipeline, type Provider } from "../lib/api";
import { Badge, Button, Card, EmptyState, ErrorText, Field, Input, Modal, Select, Td, Textarea, Th } from "../components/ui";

interface MemberDraft {
  model_id: string;
  temperature: string;
}

/** ICE 参数表单值（数值以字符串承载，保存时才校验/转换）。 */
interface IceParamsForm {
  max_rounds: string;
  confidence_threshold: string;
  stagnation: boolean;
  progress_comments: boolean;
}

// ICE 默认值与后端 IceStrategy.validate_params 的填充值保持一致（§3.2）
const ICE_DEFAULTS: IceParamsForm = {
  max_rounds: "3",
  confidence_threshold: "0.8",
  stagnation: true,
  progress_comments: true,
};

const readIceParams = (params: Record<string, unknown>): IceParamsForm => ({
  max_rounds: params.max_rounds !== undefined ? String(params.max_rounds) : ICE_DEFAULTS.max_rounds,
  confidence_threshold:
    params.confidence_threshold !== undefined
      ? String(params.confidence_threshold)
      : ICE_DEFAULTS.confidence_threshold,
  stagnation: params.stagnation !== false,
  progress_comments: params.progress_comments !== false,
});

// 上游 ID 与展示名相同时不重复拼接，避免下拉选项无谓加长
const modelOptionLabel = (model: ModelRow) => {
  const base =
    model.display_name === model.upstream_model_id
      ? model.display_name
      : `${model.display_name}（${model.upstream_model_id}）`;
  return model.upstream_missing ? `${base}【上游已下线】` : base;
};

/** 供应商→模型两级联动：先选供应商缩小范围，再在其模型中挑选，避免全量列表过长并消歧同名模型。 */
function ProviderModelSelects({
  providers,
  models,
  value,
  onModelChange,
  providerAria,
  modelAria,
}: {
  providers: Provider[];
  models: ModelRow[];
  value: string;
  onModelChange: (modelId: string) => void;
  providerAria: string;
  modelAria: string;
}) {
  // 供应商选择需要有状态承载（新选时模型尚未确定，无法从 value 反推）；回显时优先取已选模型所属供应商
  const [pickedProvider, setPickedProvider] = useState("");
  const selected = models.find((m: ModelRow) => String(m.id) === value);
  const derivedProvider = selected ? String(selected.provider_id) : "";
  const providerId = pickedProvider || derivedProvider;
  // 上游已下线的模型不进选项（防止新配置引用过时模型）；已选中的仍保留展示，便于用户识别并更换
  const scopedModels = providerId
    ? models.filter(
        (m: ModelRow) =>
          String(m.provider_id) === providerId && (!m.upstream_missing || String(m.id) === value),
      )
    : [];
  const providerOptions = providers.filter((p: Provider) => models.some((m: ModelRow) => m.provider_id === p.id));
  return (
    <>
      <Select
        className="w-28 shrink-0"
        aria-label={providerAria}
        value={providerId}
        onChange={(e) => {
          setPickedProvider(e.target.value);
          if (derivedProvider !== e.target.value) onModelChange("");
        }}
      >
        <option value="">请选择…</option>
        {providerOptions.map((p: Provider) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </Select>
      <Select
        className="min-w-0 flex-1"
        aria-label={modelAria}
        value={value}
        disabled={!providerId}
        onChange={(e) => onModelChange(e.target.value)}
      >
        <option value="">{providerId ? "请选择…" : "请先选择供应商"}</option>
        {scopedModels.map((m: ModelRow) => (
          <option key={m.id} value={m.id}>
            {modelOptionLabel(m)}
          </option>
        ))}
      </Select>
    </>
  );
}

interface FormState {
  name: string;
  strategy: string;
  judge_model_id: string;
  judge_prompt_template: string;
  stream_process: boolean;
  tool_aggregation: boolean;
  members: MemberDraft[];
  ice: IceParamsForm;
}

export default function Pipelines() {
  const queryClient = useQueryClient();
  const { data: pipelines = [], isPending } = useQuery({ queryKey: ["pipelines"], queryFn: api.pipelines.list });
  const { data: models = [] } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const { data: providers = [] } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const { data: strategies = [] } = useQuery({ queryKey: ["strategies"], queryFn: api.meta.strategies });
  const [editing, setEditing] = useState<Pipeline | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>({ name: "", strategy: "council", judge_model_id: "", judge_prompt_template: "", stream_process: false, tool_aggregation: false, members: [{ model_id: "", temperature: "" }], ice: ICE_DEFAULTS });
  const [error, setError] = useState("");
  const [defaultTemplate, setDefaultTemplate] = useState("");

  useEffect(() => {
    api.meta.judgeTemplate().then((data) => setDefaultTemplate(data.template)).catch(() => undefined);
  }, []);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["pipelines"] });
  const modelName = (id: number) => models.find((m: ModelRow) => m.id === id)?.display_name ?? `#${id}`;

  const buildBody = () => {
    const body: Record<string, unknown> = {
      name: form.name,
      strategy: form.strategy,
      judge_model_id: Number(form.judge_model_id),
      judge_prompt_template: form.judge_prompt_template,
      stream_process: form.stream_process,
      tool_aggregation: form.tool_aggregation,
      members: form.members
        .filter((member) => member.model_id)
        .map((member) => ({
          model_id: Number(member.model_id),
          ...(member.temperature ? { param_overrides: { temperature: Number(member.temperature) } } : {}),
        })),
    };
    // 仅 ICE 携带参数；council 提交空 strategy_params（后端 council 拒绝非空参数）
    if (form.strategy === "ice") {
      body.strategy_params = {
        max_rounds: Number(form.ice.max_rounds),
        confidence_threshold: Number(form.ice.confidence_threshold),
        stagnation: form.ice.stagnation,
        progress_comments: form.ice.progress_comments,
      };
    }
    return body;
  };

  const save = useMutation({
    mutationFn: async () => {
      if (!form.judge_model_id) throw new Error("必须选择裁判模型");
      if (form.members.filter((member) => member.model_id).length === 0) throw new Error("至少选择一个成员模型");
      if (form.strategy === "ice") {
        const maxRounds = Number(form.ice.max_rounds);
        if (!Number.isInteger(maxRounds) || maxRounds < 1 || maxRounds > 5) {
          throw new Error("max_rounds 必须是 1-5 的整数");
        }
        const threshold = Number(form.ice.confidence_threshold);
        if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) {
          throw new Error("confidence_threshold 必须在 0-1 之间");
        }
      }
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
    setForm({ name: "", strategy: "council", judge_model_id: "", judge_prompt_template: defaultTemplate, stream_process: false, tool_aggregation: false, members: [{ model_id: "", temperature: "" }], ice: ICE_DEFAULTS });
    setError("");
    setCreating(true);
  };
  const openEdit = (pipeline: Pipeline) => {
    setCreating(false);
    setEditing(pipeline);
    setForm({
      name: pipeline.name,
      strategy: pipeline.strategy,
      judge_model_id: String(pipeline.judge_model_id),
      judge_prompt_template: pipeline.judge_prompt_template || defaultTemplate,
      stream_process: pipeline.stream_process,
      tool_aggregation: pipeline.tool_aggregation,
      members: pipeline.members.map((member) => ({
        model_id: String(member.model_id),
        temperature: member.param_overrides.temperature !== undefined ? String(member.param_overrides.temperature) : "",
      })),
      ice: readIceParams(pipeline.strategy_params ?? {}),
    });
    setError("");
  };

  // 切换策略类型时联动重置参数区：ice→council 后提交不带 strategy_params，避免后端 422
  const changeStrategy = (strategy: string) => setForm({ ...form, strategy, ice: ICE_DEFAULTS });

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
                    {pipeline.stream_process ? <Badge tone="warn">过程流式</Badge> : null}
                    {pipeline.tool_aggregation ? <Badge tone="success">工具聚合</Badge> : null}
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
        size="lg"
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
            <Field label="聚合策略" hint="ice = 迭代共识集成（多轮成员改进 + 仲裁）">
              {strategies.length > 0 && !strategies.includes(form.strategy) ? (
                // 未知策略（如后端已下线）只读兜底展示，避免误改成其他策略
                <Input value={form.strategy} readOnly disabled aria-label="聚合策略" />
              ) : (
                <Select aria-label="聚合策略" value={form.strategy} onChange={(e) => changeStrategy(e.target.value)}>
                  {(strategies.length > 0 ? strategies : ["council"]).map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <div className="space-y-1">
              <p className="text-sm font-medium text-slate-700">裁判模型</p>
              <div className="flex items-center gap-2">
                <ProviderModelSelects
                  providers={providers}
                  models={models}
                  value={form.judge_model_id}
                  onModelChange={(modelId) => setForm({ ...form, judge_model_id: modelId })}
                  providerAria="裁判供应商"
                  modelAria="裁判模型"
                />
              </div>
              <span className="block text-xs text-slate-400">必选</span>
            </div>
          </div>

          <label className="flex items-center gap-1.5 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={form.stream_process}
              onChange={(e) => setForm({ ...form, stream_process: e.target.checked })}
            />
            过程流式输出（reasoning_content）
          </label>

          <label className="flex items-center gap-1.5 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={form.tool_aggregation}
              onChange={(e) => setForm({ ...form, tool_aggregation: e.target.checked })}
            />
            工具聚合（接受 tools，裁判综合成员调用意图合成最终调用）
          </label>

          {form.strategy === "ice" ? (            <div className="space-y-2 rounded-md border border-slate-200 p-3" data-testid="ice-params">
              <p className="text-sm font-medium text-slate-700">ICE 策略参数</p>
              <div className="grid grid-cols-2 gap-3">
                <Field label="max_rounds（成员轮数上限）" hint="整数 1-5，含第 0 轮">
                  <Input
                    inputMode="numeric"
                    value={form.ice.max_rounds}
                    onChange={(e) => setForm({ ...form, ice: { ...form.ice, max_rounds: e.target.value } })}
                  />
                </Field>
                <Field label="confidence_threshold（共识置信度阈值）" hint="0-1，consensus=true 且不低于阈值才终局">
                  <Input
                    inputMode="decimal"
                    value={form.ice.confidence_threshold}
                    onChange={(e) => setForm({ ...form, ice: { ...form.ice, confidence_threshold: e.target.value } })}
                  />
                </Field>
              </div>
              <div className="flex gap-6">
                <label className="flex items-center gap-1.5 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={form.ice.stagnation}
                    onChange={(e) => setForm({ ...form, ice: { ...form.ice, stagnation: e.target.checked } })}
                  />
                  stagnation（置信度停滞提前终局）
                </label>
                <label className="flex items-center gap-1.5 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={form.ice.progress_comments}
                    onChange={(e) => setForm({ ...form, ice: { ...form.ice, progress_comments: e.target.checked } })}
                  />
                  progress_comments（流式发送进度注释行）
                </label>
              </div>
            </div>
          ) : null}

          <div className="space-y-2">
            <p className="text-sm font-medium text-slate-700">成员模型（按顺序并发调用）</p>
            {form.members.map((member, index) => (
              <div key={index} className="flex items-center gap-2">
                <span className="w-5 shrink-0 text-xs text-slate-400">#{index + 1}</span>
                <ProviderModelSelects
                  providers={providers}
                  models={models}
                  value={member.model_id}
                  onModelChange={(modelId) => {
                    const members = [...form.members];
                    members[index] = { ...member, model_id: modelId };
                    setForm({ ...form, members });
                  }}
                  providerAria={`成员${index + 1}供应商`}
                  modelAria={`成员${index + 1}模型`}
                />
                <Input
                  className="w-24 shrink-0"
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

          <Field
            label="裁判 Prompt 模板（最终指令轮）"
            hint="两段式裁判（同一会话）：先由裁判评论各答案（内置模板），再以第二次调用延续会话产出最终答案，本模板即最后的指令轮；评论已作为会话上下文自动带入，占位符 {{original_messages}} / {{candidate_answers}} / {{critique}} 仅在指令中重述时使用，留空使用默认指令"
          >
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
