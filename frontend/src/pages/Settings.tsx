import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ServiceSettings } from "../lib/api";
import { formatBytes, formatDateTime } from "../lib/trace";
import { Badge, Button, Card, ErrorText, Field, Input, Modal } from "../components/ui";

export default function Settings() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings.get });
  const { data: serviceKey } = useQuery({
    queryKey: ["service-key"],
    queryFn: api.settings.serviceKey,
  });
  const { data: pipelines = [] } = useQuery({ queryKey: ["pipelines"], queryFn: api.pipelines.list });
  const { data: models = [] } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const { data: providers = [] } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const [retention, setRetention] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [copied, setCopied] = useState("");
  const [error, setError] = useState("");

  const copy = (what: string, text: string) => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(what);
      setTimeout(() => setCopied(""), 1500);
    });
  };

  const saveRetention = useMutation({
    mutationFn: () => api.settings.update({ log_retention_days: Number(retention) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const updateExposure = useMutation({
    mutationFn: (body: { expose_virtual_models?: boolean; expose_routed_models?: boolean }) =>
      api.settings.update(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const resetKey = useMutation({
    mutationFn: api.settings.resetKey,
    onSuccess: (result) => {
      setNewKey(result.service_api_key);
      void queryClient.invalidateQueries({ queryKey: ["service-key"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const info: ServiceSettings | undefined = data;
  const gatewayUrl = `${window.location.origin}/v1`;
  const enabledPipelines = pipelines.filter((p) => p.enabled).length;
  const enabledModels = models.filter((m) => m.enabled).length;

  const exposureRow = (
    title: string,
    description: string,
    exposed: boolean,
    enabledCount: number,
    body: { expose_virtual_models?: boolean; expose_routed_models?: boolean },
    children?: ReactNode,
  ) => (
    <div className="rounded-lg border border-slate-100 p-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <p className="text-sm font-medium">{title}</p>
            <Badge tone={exposed ? "success" : "muted"}>{exposed ? "接入开启" : "接入关闭"}</Badge>
          </div>
          <p className="mt-1 text-xs text-slate-500">{description}</p>
          <p className="mt-1 text-xs text-slate-400">
            启用中 {enabledCount} 个
            {exposed ? "，将出现在 /v1/models" : "，已从 /v1/models 隐藏且不可调用"}
          </p>
        </div>
        <Button
          variant="secondary"
          disabled={updateExposure.isPending}
          onClick={() => updateExposure.mutate(body)}
        >
          {exposed ? "关闭接入" : "开启接入"}
        </Button>
      </div>
      {children}
    </div>
  );

  const urlRow = (url: string, hint: string, disabled = false) => (
    <div className="mt-2 flex items-center gap-2">
      <code
        className={`flex-1 truncate rounded px-2 py-1 font-mono text-xs ${
          disabled ? "bg-slate-50 text-slate-400" : "bg-slate-100"
        }`}
      >
        {url}
      </code>
      <span className="w-40 shrink-0 text-xs text-slate-400">{disabled ? `${hint}（停用）` : hint}</span>
      <Button variant="ghost" disabled={disabled} onClick={() => copy(url, url)}>
        {copied === url ? "已复制 ✓" : "复制"}
      </Button>
    </div>
  );

  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-lg font-bold">设置</h1>
      <ErrorText>{error}</ErrorText>

      <Card title="接入信息">
        <p className="mb-3 text-sm text-slate-500">
          客户端按 OpenAI 兼容协议接入：base_url 填总地址（下方）或任一命名空间地址，认证用下方
          服务 API Key；两类模型有独立开关。
        </p>
        <div className="flex items-center gap-2">
          <code className="flex-1 break-all rounded bg-slate-900 px-3 py-2 font-mono text-sm text-emerald-300">
            {gatewayUrl}
          </code>
          <Button variant="secondary" onClick={() => copy("url", gatewayUrl)}>
            复制地址
          </Button>
        </div>
        {copied === "url" ? <p className="mt-1 text-xs text-emerald-600">已复制 ✓</p> : null}
        {info ? (
          <div className="mt-4 space-y-3">
            {exposureRow(
              "虚拟模型（Pipeline）",
              "base_url 填 /v1/pipeline，model 填 Pipeline 名（如 council-v1）：多模型并行 + 裁判聚合。成员与裁判在“流水线”页配置。",
              info.expose_virtual_models,
              enabledPipelines,
              { expose_virtual_models: !info.expose_virtual_models },
              urlRow(`${gatewayUrl}/pipeline`, "虚拟模型命名空间", !info.expose_virtual_models),
            )}
            {exposureRow(
              "路由模型（透传）",
              "base_url 填各供应商的 /v1/route/{slug}，model 直接填真实模型名：请求透传上游，不聚合。模型在“模型”页配置。",
              info.expose_routed_models,
              enabledModels,
              { expose_routed_models: !info.expose_routed_models },
              <div className={info.expose_routed_models ? "" : "opacity-60"}>
                {providers.length === 0 ? (
                  <p className="mt-2 text-xs text-slate-400">暂无供应商，先到“供应商”页接入</p>
                ) : (
                  providers.map((provider) =>
                    urlRow(
                      `${gatewayUrl}/route/${provider.slug}`,
                      `${provider.name}（也可用 /route/${provider.id}）`,
                      !info.expose_routed_models || !provider.enabled,
                    )
                  )
                )}
              </div>,
            )}
          </div>
        ) : (
          <p className="mt-4 text-sm text-slate-400">加载中…</p>
        )}
      </Card>

      <Card title="服务 API Key（对外 /v1 接口认证）">
        <div className="mb-2 flex items-center gap-2">
          <code className="flex-1 truncate rounded bg-slate-100 px-3 py-2 font-mono text-sm">
            {serviceKey?.available
              ? serviceKey.masked
              : "（旧版数据库无加密副本，重置一次后即可随时复制）"}
          </code>
          <Button
            variant="secondary"
            disabled={!serviceKey?.available}
            onClick={() =>
              serviceKey?.service_api_key && copy("key", serviceKey.service_api_key)
            }
          >
            复制 Key
          </Button>
        </div>
        {copied === "key" ? <p className="mb-2 text-xs text-emerald-600">已复制 ✓</p> : null}
        <p className="mb-3 text-sm text-slate-500">
          认证按哈希比对；重置后旧 Key 立即失效，新 Key 自动同步到本页。
        </p>
        <Button
          variant="danger"
          onClick={() => {
            if (confirm("确认重置服务 API Key？旧 Key 将立即失效。")) resetKey.mutate();
          }}
        >
          重置服务 Key
        </Button>
      </Card>

      <Card title="服务信息">
        {info ? (
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-slate-400">版本</dt>
              <dd>{info.version}</dd>
            </div>
            <div>
              <dt className="text-slate-400">启动时间</dt>
              <dd>{formatDateTime(info.started_at)}</dd>
            </div>
            <div>
              <dt className="text-slate-400">数据库</dt>
              <dd className="font-mono text-xs">{info.database_path}</dd>
            </div>
            <div>
              <dt className="text-slate-400">存储占用</dt>
              <dd>{formatBytes(info.database_size_bytes)}</dd>
            </div>
          </dl>
        ) : (
          <p className="text-sm text-slate-400">加载中…</p>
        )}
      </Card>

      <Card title="日志保留">
        {info ? (
          <div className="flex items-end gap-3">
            <div className="w-40">
              <Field label="保留天数">
                <Input
                  type="number"
                  min={1}
                  placeholder={String(info.log_retention_days)}
                  value={retention}
                  onChange={(e) => setRetention(e.target.value)}
                />
              </Field>
            </div>
            <Button
              disabled={!retention || Number(retention) < 1}
              onClick={() => saveRetention.mutate()}
            >
              保存
            </Button>
            <span className="text-xs text-slate-400">当前：{info.log_retention_days} 天（服务启动时自动清理过期日志）</span>
            {saveRetention.isSuccess ? <span className="text-xs text-emerald-600">已保存 ✓</span> : null}
          </div>
        ) : null}
      </Card>

      <Modal open={newKey !== null} title="新服务 API Key（仅显示一次）">
        <p className="mb-2 text-sm text-red-600">请立即复制保存，关闭后将无法再次查看。</p>
        <pre className="break-all rounded bg-slate-900 p-3 text-sm text-emerald-300">{newKey}</pre>
        <div className="mt-3 flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              if (newKey) void navigator.clipboard?.writeText(newKey);
            }}
          >
            复制
          </Button>
          <Button onClick={() => setNewKey(null)}>我已保存，关闭</Button>
        </div>
      </Modal>
    </div>
  );
}
