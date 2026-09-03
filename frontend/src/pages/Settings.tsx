import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ServiceSettings } from "../lib/api";
import { formatBytes, formatDateTime } from "../lib/trace";
import { Button, Card, ErrorText, Field, Input, Modal } from "../components/ui";

export default function Settings() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings.get });
  const { data: serviceKey } = useQuery({
    queryKey: ["service-key"],
    queryFn: api.settings.serviceKey,
  });
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

  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-lg font-bold">设置</h1>
      <ErrorText>{error}</ErrorText>

      <Card title="接入信息（虚拟模型）">
        <p className="mb-3 text-sm text-slate-500">
          客户端按 OpenAI 兼容协议接入：base_url 填下面的地址，model 填 Pipeline（虚拟模型）名，
          认证用下方服务 API Key。
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
