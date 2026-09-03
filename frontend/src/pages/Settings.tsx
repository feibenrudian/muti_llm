import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type ServiceSettings } from "../lib/api";
import { formatBytes, formatDateTime } from "../lib/trace";
import { Button, Card, ErrorText, Field, Input, Modal } from "../components/ui";

export default function Settings() {
  const queryClient = useQueryClient();
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings.get });
  const [retention, setRetention] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [error, setError] = useState("");

  const saveRetention = useMutation({
    mutationFn: () => api.settings.update({ log_retention_days: Number(retention) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings"] }),
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const resetKey = useMutation({
    mutationFn: api.settings.resetKey,
    onSuccess: (result) => setNewKey(result.service_api_key),
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const info: ServiceSettings | undefined = data;

  return (
    <div className="max-w-3xl space-y-4">
      <h1 className="text-lg font-bold">设置</h1>
      <ErrorText>{error}</ErrorText>

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

      <Card title="服务 API Key（对外 /v1 接口认证）">
        <p className="mb-3 text-sm text-slate-500">
          Key 仅存哈希，无法找回；重置后旧 Key 立即失效，新明文只展示一次，请立即保存。
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
