/** 门户 API Key 自助管理（T52）：弹窗签发（名称可选）/ 复制 / 启停 / 删除 + 只读"可调用范围"。
 *
 * 明文不单独弹窗展示：列表"复制"随时可经 Fernet 副本取回明文（产品决策，创建流程从简）。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type UserKeyRow } from "../lib/api";
import { formatDateTime } from "../lib/trace";
import { isKeyStale } from "../lib/users";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorText,
  Field,
  Input,
  Modal,
  Td,
  Th,
} from "../components/ui";

export default function PortalKeys() {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const { data: keys = [], isPending } = useQuery({
    queryKey: ["portal-keys"],
    queryFn: api.portal.keys.list,
  });
  const { data: grants } = useQuery({ queryKey: ["portal-grants"], queryFn: api.portal.grants });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["portal-keys"] });
  const updateKey = useMutation({
    mutationFn: ({ keyId, body }: { keyId: number; body: { enabled?: boolean } }) =>
      api.portal.keys.update(keyId, body),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const deleteKey = useMutation({
    mutationFn: (keyId: number) => api.portal.keys.remove(keyId),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  return (
    <div className="grid grid-cols-3 gap-4">
      <div className="col-span-2">
        <Card
          title="我的 API Key"
          actions={
            <Button onClick={() => setCreateOpen(true)} data-testid="portal-btn-new-key">
              新建 Key
            </Button>
          }
        >
          <ErrorText>{error}</ErrorText>
          {isPending ? (
            <EmptyState>加载中…</EmptyState>
          ) : keys.length === 0 ? (
            <EmptyState>还没有 Key，点右上角"新建 Key"签发</EmptyState>
          ) : (
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-100">
                  <Th>名称</Th>
                  <Th>Key</Th>
                  <Th>状态</Th>
                  <Th>最后使用</Th>
                  <Th>创建时间</Th>
                  <Th>操作</Th>
                </tr>
              </thead>
              <tbody>
                {keys.map((row) => (
                  <tr key={row.id} className="border-b border-slate-50" data-testid="portal-key-row">
                    <Td>
                      {row.name || "—"}{" "}
                      {isKeyStale(row.last_used_at, row.created_at) ? (
                        <Badge tone="warn">长期未使用</Badge>
                      ) : null}
                    </Td>
                    <Td>
                      <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs">
                        {row.masked}
                      </code>
                    </Td>
                    <Td>
                      {row.enabled ? (
                        <Badge tone="success">启用</Badge>
                      ) : (
                        <Badge tone="danger">停用</Badge>
                      )}
                    </Td>
                    <Td>{row.last_used_at ? formatDateTime(row.last_used_at) : "—"}</Td>
                    <Td>{formatDateTime(row.created_at)}</Td>
                    <Td>
                      <span className="flex gap-1">
                        <CopyKeyButton row={row} onError={setError} />
                        <Button
                          variant="ghost"
                          onClick={() =>
                            updateKey.mutate({ keyId: row.id, body: { enabled: !row.enabled } })
                          }
                        >
                          {row.enabled ? "停用" : "启用"}
                        </Button>
                        <Button
                          variant="danger"
                          onClick={() => {
                            if (confirm("确认删除该 Key？使用它的客户端将立即 401。"))
                              deleteKey.mutate(row.id);
                          }}
                        >
                          删除
                        </Button>
                      </span>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <Card title="可调用范围">
        <p className="mb-3 text-xs text-slate-400">
          由管理员授权（只读）。调用未授权的模型会返回 model_not_authorized。
        </p>
        <GrantSection label="组合（Pipeline）" items={grants?.pipelines ?? []} testId="portal-grant-pipelines" />
        <GrantSection label="模型" items={grants?.models ?? []} testId="portal-grant-models" />
        <GrantSection label="供应商" items={grants?.providers ?? []} testId="portal-grant-providers" />
      </Card>

      <CreateKeyModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}

/** 创建弹窗：名称可选；成功后回列表（明文随时点"复制"获取）。 */
function CreateKeyModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  const create = useMutation({
    mutationFn: () => api.portal.keys.create(name),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["portal-keys"] });
      close();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const close = () => {
    setName("");
    setError("");
    onClose();
  };

  return (
    <Modal open={open} title="新建 API Key" onClose={close}>
      <div className="space-y-3">
        <ErrorText>{error}</ErrorText>
        <Field label="名称（可选）" hint="便于识别用途，如：家用笔记本、CI 脚本">
          <Input
            value={name}
            placeholder="家用笔记本"
            onChange={(e) => setName(e.target.value)}
            data-testid="portal-input-key-name"
          />
        </Field>
        <p className="text-xs text-slate-400">创建后可随时在列表点"复制"获取 Key 明文。</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={close} data-testid="portal-btn-cancel-create">
            取消
          </Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending} data-testid="portal-btn-create-key">
            {create.isPending ? "创建中…" : "创建"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function GrantSection({
  label,
  items,
  testId,
}: {
  label: string;
  items: { id: number; name: string }[];
  testId: string;
}) {
  return (
    <div className="mb-3" data-testid={testId}>
      <p className="text-xs font-medium text-slate-500">{label}</p>
      {items.length === 0 ? (
        <p className="text-sm text-slate-400">（未授权）</p>
      ) : (
        <ul className="mt-1 space-y-0.5">
          {items.map((item) => (
            <li key={item.id} className="text-sm">
              {item.name}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CopyKeyButton({
  row,
  onError,
}: {
  row: UserKeyRow;
  onError: (message: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = useMutation({
    mutationFn: () => api.portal.keys.keySecret(row.id),
    onSuccess: ({ api_key }) => {
      void navigator.clipboard?.writeText(api_key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : String(err)),
  });
  return (
    <Button variant="ghost" onClick={() => copy.mutate()} data-testid={`portal-btn-copy-key-${row.id}`}>
      {copied ? "已复制 ✓" : "复制"}
    </Button>
  );
}
