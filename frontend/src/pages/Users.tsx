/** 用户管理页（多用户 T47）：列表 / 新建 / 编辑 / 启停 / 删除；详情与 Key/授权见 UserDetail。 */

import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type UserRow } from "../lib/api";
import { formatDateTime } from "../lib/trace";
import { validateUsername } from "../lib/users";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorText,
  Field,
  Input,
  Modal,
  Select,
  Td,
  Th,
} from "../components/ui";

export default function Users() {
  const queryClient = useQueryClient();
  const [keyword, setKeyword] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [error, setError] = useState("");
  const { data, isPending, isError, error: queryError } = useQuery({
    queryKey: ["users", keyword, roleFilter],
    queryFn: () =>
      api.users.list({
        keyword: keyword || undefined,
        role: roleFilter || undefined,
        page: 1,
        page_size: 100,
      }),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["users"] });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api.users.update(id, body),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.users.remove(id),
    onSuccess: invalidate,
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const toggle = (row: UserRow) => update.mutate({ id: row.id, body: { enabled: !row.enabled } });

  return (
    <div className="max-w-4xl space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">用户</h1>
        <Button onClick={() => setCreateOpen(true)} data-testid="btn-create-user">
          新建用户
        </Button>
      </div>
      <ErrorText>{error || (isError ? queryError.message : "")}</ErrorText>

      <Card>
        <div className="mb-3 flex gap-2">
          <div className="w-56">
            <Input
              placeholder="用户名 / 显示名搜索"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              data-testid="input-user-keyword"
            />
          </div>
          <div className="w-32">
            <Select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
              <option value="">全部角色</option>
              <option value="admin">管理员</option>
              <option value="user">一般用户</option>
            </Select>
          </div>
        </div>
        {isPending ? (
          <EmptyState>加载中…</EmptyState>
        ) : !data || data.items.length === 0 ? (
          <EmptyState>暂无用户</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>用户名</Th>
                <Th>显示名</Th>
                <Th>角色</Th>
                <Th>状态</Th>
                <Th>Key 数</Th>
                <Th>最后活动</Th>
                <Th>创建时间</Th>
                <Th>操作</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((row) => (
                <tr key={row.id} className="border-b border-slate-50">
                  <Td>
                    <Link
                      to={`/users/${row.id}`}
                      className="font-mono text-slate-900 hover:underline"
                      data-testid={`user-link-${row.username}`}
                    >
                      {row.username}
                    </Link>
                  </Td>
                  <Td>{row.display_name || "—"}</Td>
                  <Td>
                    {row.role === "admin" ? <Badge tone="warn">管理员</Badge> : <Badge>用户</Badge>}
                  </Td>
                  <Td>
                    {row.enabled ? (
                      <Badge tone="success">启用</Badge>
                    ) : (
                      <Badge tone="danger">停用</Badge>
                    )}
                  </Td>
                  <Td>{row.key_count}</Td>
                  <Td>{row.last_activity_at ? formatDateTime(row.last_activity_at) : "—"}</Td>
                  <Td>{formatDateTime(row.created_at)}</Td>
                  <Td>
                    <span className="flex gap-1">
                      <Button variant="ghost" onClick={() => toggle(row)}>
                        {row.enabled ? "停用" : "启用"}
                      </Button>
                      <Button
                        variant="danger"
                        onClick={() => {
                          if (confirm(`确认删除用户 ${row.username}？其 Key 与授权将一并删除。`))
                            remove.mutate(row.id);
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

      <CreateUserModal open={createOpen} onClose={() => setCreateOpen(false)} />
    </div>
  );
}

function CreateUserModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState("user");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const create = useMutation({
    mutationFn: () =>
      api.users.create({
        username,
        display_name: displayName,
        role,
        ...(password ? { password } : {}),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      onClose();
      setUsername("");
      setDisplayName("");
      setPassword("");
      setError("");
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : String(err)),
  });

  const usernameError = username ? validateUsername(username) : "";
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (usernameError) {
      setError(usernameError);
      return;
    }
    create.mutate();
  };

  return (
    <Modal open={open} title="新建用户" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3">
        <ErrorText>{error || usernameError}</ErrorText>
        <Field label="用户名" hint="3-32 位字母/数字/._-，创建后不可改">
          <Input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            data-testid="input-new-username"
          />
        </Field>
        <Field label="显示名">
          <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </Field>
        <Field label="角色">
          <Select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="user">一般用户</option>
            <option value="admin">管理员（需设置初始密码）</option>
          </Select>
        </Field>
        <Field
          label={role === "admin" ? "初始密码" : "初始密码（可选，用于登录自助门户）"}
          hint="至少 8 位；也可创建后在用户详情页设置"
        >
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            data-testid="input-new-password"
          />
        </Field>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            取消
          </Button>
          <Button
            type="submit"
            disabled={!username || Boolean(usernameError) || (role === "admin" && password.length < 8)}
            data-testid="btn-submit-user"
          >
            创建
          </Button>
        </div>
      </form>
    </Modal>
  );
}
