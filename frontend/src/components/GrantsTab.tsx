/** 模型权限 Tab（多用户 T49）：虚拟模型 / 供应商 / 模型三组勾选，取并集生效（FR-19）。
 *
 * 含停用与已下线项（置灰可勾选，便于预配置）；运行期以启用状态为准。
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError, type GrantsPayload, type ModelRow } from "../lib/api";
import { emptySelection, selectionEqualsPayload, selectionToPayload, type GrantsSelection } from "../lib/users";
import { Button, Card, ErrorText } from "./ui";

interface Props {
  userId: number;
  onSaved?: () => void;
}

export default function GrantsTab({ userId, onSaved }: Props) {
  const [selection, setSelection] = useState<GrantsSelection>(emptySelection());
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const { data: grants } = useQuery({
    queryKey: ["user-grants", userId],
    queryFn: () => api.users.grants(userId),
  });
  const { data: pipelines = [] } = useQuery({ queryKey: ["pipelines"], queryFn: api.pipelines.list });
  const { data: models = [] } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const { data: providers = [] } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });

  useEffect(() => {
    if (!grants) return;
    setSelection({
      pipelines: new Set(grants.pipelines),
      models: new Set(grants.models),
      providers: new Set(grants.providers),
    });
    setDirty(false);
  }, [grants]);

  const save = useMutation({
    mutationFn: (payload: GrantsPayload) => api.users.setGrants(userId, payload),
    onSuccess: () => {
      setSaved(true);
      setDirty(false);
      setTimeout(() => setSaved(false), 1500);
      onSaved?.();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const toggle = (group: keyof GrantsSelection, id: number) => {
    setSelection((prev) => {
      const next = { pipelines: new Set(prev.pipelines), models: new Set(prev.models), providers: new Set(prev.providers) };
      if (next[group].has(id)) next[group].delete(id);
      else next[group].add(id);
      return next;
    });
    setDirty(true);
    setSaved(false);
  };

  const setAll = (group: keyof GrantsSelection, ids: number[], checked: boolean) => {
    setSelection((prev) => {
      const next = { pipelines: new Set(prev.pipelines), models: new Set(prev.models), providers: new Set(prev.providers) };
      for (const id of ids) {
        if (checked) next[group].add(id);
        else next[group].delete(id);
      }
      return next;
    });
    setDirty(true);
    setSaved(false);
  };

  const payload = selectionToPayload(selection);
  const pristine = grants
    ? selectionEqualsPayload(selection, grants)
    : true;

  return (
    <Card
      title="模型权限（三类取并集；默认拒绝，保存后即时生效）"
      actions={
        <span className="flex items-center gap-2">
          {saved ? <span className="text-xs text-emerald-600">已保存 ✓</span> : null}
          <Button
            onClick={() => save.mutate(payload)}
            disabled={!dirty || pristine || save.isPending}
            data-testid="btn-save-grants"
          >
            保存授权
          </Button>
        </span>
      }
    >
      <ErrorText>{error}</ErrorText>
      <GrantGroup
        title="虚拟模型（Pipeline）"
        hint="允许调用该聚合入口；成员/裁判模型无需单独授权"
        rows={pipelines.map((p) => ({ id: p.id, label: p.name, enabled: p.enabled, missing: false }))}
        selected={selection.pipelines}
        onToggle={(id) => toggle("pipelines", id)}
        onAll={(checked) => setAll("pipelines", pipelines.map((p) => p.id), checked)}
        testidPrefix="grant-pipeline"
      />
      <GrantGroup
        title="供应商（覆盖其全部模型，含上游新增）"
        hint="供应商级授权是调用上游新增动态模型的唯一途径"
        rows={providers.map((p) => ({ id: p.id, label: p.name, enabled: p.enabled, missing: false }))}
        selected={selection.providers}
        onToggle={(id) => toggle("providers", id)}
        onAll={(checked) => setAll("providers", providers.map((p) => p.id), checked)}
        testidPrefix="grant-provider"
      />
      <GrantGroup
        title="模型（精确到单个模型行）"
        hint="覆盖老入口透传与 route 命名空间；已下线模型置灰可勾选（预配置）"
        rows={models.map((m: ModelRow) => ({
          id: m.id,
          label: `${m.upstream_model_id}（${m.display_name}）`,
          enabled: m.enabled && !m.upstream_missing,
          missing: m.upstream_missing,
        }))}
        selected={selection.models}
        onToggle={(id) => toggle("models", id)}
        onAll={(checked) => setAll("models", models.map((m) => m.id), checked)}
        testidPrefix="grant-model"
      />
    </Card>
  );
}

function GrantGroup({
  title,
  hint,
  rows,
  selected,
  onToggle,
  onAll,
  testidPrefix,
}: {
  title: string;
  hint: string;
  rows: { id: number; label: string; enabled: boolean; missing: boolean }[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  onAll: (checked: boolean) => void;
  testidPrefix: string;
}) {
  const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.id));
  return (
    <div className="mt-4 rounded-lg border border-slate-100 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div>
          <p className="text-sm font-medium">{title}</p>
          <p className="text-xs text-slate-400">{hint}</p>
        </div>
        <Button variant="ghost" onClick={() => onAll(!allChecked)} data-testid={`${testidPrefix}-all`}>
          {allChecked ? "清空" : "全选"}
        </Button>
      </div>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-400">暂无可选项</p>
      ) : (
        <div className="grid grid-cols-2 gap-1 md:grid-cols-3">
          {rows.map((row) => (
            <label
              key={row.id}
              className={`flex items-center gap-2 rounded px-2 py-1 text-sm hover:bg-slate-50 ${
                row.enabled ? "" : "text-slate-400"
              }`}
            >
              <input
                type="checkbox"
                checked={selected.has(row.id)}
                onChange={() => onToggle(row.id)}
                data-testid={`${testidPrefix}-${row.id}`}
              />
              <span className="truncate">{row.label}</span>
              {row.missing ? <span className="text-xs text-amber-600">已下线</span> : null}
              {!row.enabled && !row.missing ? <span className="text-xs">已停用</span> : null}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
