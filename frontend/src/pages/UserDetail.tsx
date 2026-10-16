/** 用户详情页（多用户 T47/T49/T52）：API Key 管理 + 模型权限两个 Tab + 口令重置。 */

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
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
import GrantsTab from "../components/GrantsTab";

export default function UserDetail() {
  const { id } = useParams();
  const userId = Number(id);
  const queryClient = useQueryClient();
  const { data: user } = useQuery({
    queryKey: ["users"],
    queryFn: () => api.users.list({ page: 1, page_size: 200 }),
    select: (data) => data.items.find((u) => u.id === userId),
  });
  const [tab, setTab] = useState<"keys" | "grants">("keys");
  const [resetOpen, setResetOpen] = useState(false);

  if (!user) {
    return (
      <div className="max-w-4xl space-y-4">
        <Link to="/users" className="text-sm text-slate-500 hover:underline">
          ← 返回用户列表
        </Link>
        <EmptyState>用户不存在</EmptyState>
      </div>
    );
  }

  return (
    <div className="max-w-4xl space-y-4">
      <div>
        <Link to="/users" className="text-sm text-slate-500 hover:underline">
          ← 返回用户列表
        </Link>
        <h1 className="mt-1 flex items-center gap-2 text-lg font-bold">
          {user.display_name || user.username}{" "}
          <span className="font-mono text-sm text-slate-400">{user.username}</span>{" "}
          {user.role === "admin" ? <Badge tone="warn">管理员</Badge> : <Badge>用户</Badge>}
          {user.enabled ? null : <Badge tone="danger">停用</Badge>}
          <Button
            variant="secondary"
            onClick={() => setResetOpen(true)}
            data-testid="btn-reset-password"
          >
            设置/重置密码
          </Button>
        </h1>
      </div>

      <div className="flex gap-2" role="tablist">
        {(
          [
            ["keys", "API Key"],
            ["grants", "模型权限"],
          ] as const
        ).map(([key, label]) => (
          <Button
            key={key}
            variant={tab === key ? "primary" : "secondary"}
            onClick={() => setTab(key)}
            role="tab"
            data-testid={`tab-${key}`}
          >
            {label}
          </Button>
        ))}
      </div>

      {tab === "keys" ? <KeysTab userId={userId} /> : <GrantsTab userId={userId} onSaved={() => void queryClient.invalidateQueries({ queryKey: ["users"] })} />}

      <ResetPasswordModal open={resetOpen} onClose={() => setResetOpen(false)} user={user} />
    </div>
  );
}

function ResetPasswordModal({
  open,
  onClose,
  user,
}: {
  open: boolean;
  onClose: () => void;
  user: { id: number; username: string; role: string };
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  const reset = useMutation({
    mutationFn: () => api.users.resetPassword(user.id, password),
    onSuccess: () => {
      setDone(true);
      setPassword("");
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const close = () => {
    setDone(false);
    setError("");
    setPassword("");
    onClose();
  };

  return (
    <Modal open={open} title={`设置/重置密码（${user.username}）`} onClose={close}>
      <div className="space-y-3">
        {done ? (
          <p className="text-sm text-emerald-600">
            密码已更新，该用户所有登录会话已失效。
            {user.role === "user" ? "其现在可凭新密码登录自助门户（/portal）。" : ""}
          </p>
        ) : (
          <>
            <ErrorText>{error}</ErrorText>
            <Field label="新密码" hint="至少 8 位；重置后该用户全部会话将被吊销">
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                data-testid="input-reset-password"
              />
            </Field>
            <div className="flex justify-end">
              <Button
                disabled={password.length < 8 || reset.isPending}
                onClick={() => reset.mutate()}
                data-testid="btn-submit-reset-password"
              >
                确认重置
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

function KeysTab({ userId }: { userId: number }) {
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [newKeyName, setNewKeyName] = useState("");
  const [created, setCreated] = useState<UserKeyRow & { api_key: string } | null>(null);
  const { data: keys = [], isPending } = useQuery({
    queryKey: ["user-keys", userId],
    queryFn: () => api.users.keys(userId),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["user-keys", userId] });
  const invalidateUsage = () => {
    invalidate();
    void queryClient.invalidateQueries({ queryKey: ["users"] });
  };
  const createKey = useMutation({
    mutationFn: () => api.users.createKey(userId, newKeyName),
    onSuccess: (row) => {
      setCreated(row);
      setNewKeyName("");
      invalidateUsage();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const updateKey = useMutation({
    mutationFn: ({ keyId, body }: { keyId: number; body: { enabled?: boolean } }) =>
      api.users.updateKey(userId, keyId, body),
    onSuccess: invalidateUsage,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const deleteKey = useMutation({
    mutationFn: (keyId: number) => api.users.deleteKey(userId, keyId),
    onSuccess: invalidateUsage,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  return (
    <Card
      title="API Key"
      actions={
        <span className="flex items-center gap-2">
          <Input
            placeholder="Key 名称（如：家用笔记本）"
            value={newKeyName}
            onChange={(e) => setNewKeyName(e.target.value)}
            className="!w-56"
            data-testid="input-key-name"
          />
          <Button
            onClick={() => createKey.mutate()}
            disabled={createKey.isPending}
            data-testid="btn-create-key"
          >
            新建 Key
          </Button>
        </span>
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
              <tr key={row.id} className="border-b border-slate-50" data-testid="key-row">
                <Td>
                  {row.name || "—"}{" "}
                  {row.is_legacy ? <Badge>兼容 Key</Badge> : null}
                  {!row.is_legacy && isKeyStale(row.last_used_at, row.created_at) ? (
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
                  {row.is_legacy ? (
                    <span className="text-xs text-slate-400">重置请到设置页</span>
                  ) : (
                    <span className="flex gap-1">
                      <CopyKeyButton userId={userId} row={row} onError={setError} />
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
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <Modal open={created !== null} title="新 API Key（仅显示一次）">
        <p className="mb-2 text-sm text-red-600">请立即复制保存，关闭后将无法再次查看明文。</p>
        <pre
          className="break-all rounded bg-slate-900 p-3 text-sm text-emerald-300"
          data-testid="new-key-plaintext"
        >
          {created?.api_key}
        </pre>
        <div className="mt-3 flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={() => created && void navigator.clipboard?.writeText(created.api_key)}
          >
            复制
          </Button>
          <Button onClick={() => setCreated(null)} data-testid="btn-key-saved">
            我已保存，关闭
          </Button>
        </div>
      </Modal>
    </Card>
  );
}

function CopyKeyButton({
  userId,
  row,
  onError,
}: {
  userId: number;
  row: UserKeyRow;
  onError: (message: string) => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = useMutation({
    mutationFn: () => api.users.keySecret(userId, row.id),
    onSuccess: ({ api_key }) => {
      void navigator.clipboard?.writeText(api_key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : String(err)),
  });
  return (
    <Button variant="ghost" onClick={() => copy.mutate()} data-testid={`btn-copy-key-${row.id}`}>
      {copied ? "已复制 ✓" : "复制"}
    </Button>
  );
}
