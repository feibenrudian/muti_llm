import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { formatDateTime, formatDuration, statusTone } from "../lib/trace";
import { Badge, Button, Card, EmptyState, Field, Input, Select, Td, Th } from "../components/ui";

const PAGE_SIZE = 20;

export default function Traces() {
  const [filters, setFilters] = useState({ pipeline: "", status: "", model: "", keyword: "" });
  const [page, setPage] = useState(1);
  const { data, isPending } = useQuery({
    queryKey: ["traces", filters, page],
    queryFn: () => api.traces.list({ ...filters, page, page_size: PAGE_SIZE }),
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">调用日志（Trace）</h1>
      <Card title={`共 ${data?.total ?? 0} 条`}>
        <div className="mb-4 grid grid-cols-5 gap-3">
          <Field label="虚拟模型/Pipeline">
            <Input value={filters.pipeline} onChange={(e) => { setFilters({ ...filters, pipeline: e.target.value }); setPage(1); }} placeholder="council-v1" />
          </Field>
          <Field label="成员模型">
            <Input value={filters.model} onChange={(e) => { setFilters({ ...filters, model: e.target.value }); setPage(1); }} placeholder="deepseek-v4-flash" />
          </Field>
          <Field label="状态">
            <Select value={filters.status} onChange={(e) => { setFilters({ ...filters, status: e.target.value }); setPage(1); }}>
              <option value="">全部</option>
              <option value="success">success</option>
              <option value="degraded">degraded</option>
              <option value="failed">failed</option>
              <option value="client_cancelled">client_cancelled</option>
            </Select>
          </Field>
          <Field label="关键字（匹配原始输入）">
            <Input value={filters.keyword} onChange={(e) => { setFilters({ ...filters, keyword: e.target.value }); setPage(1); }} placeholder="量子" />
          </Field>
          <div className="flex items-end gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                setFilters({ pipeline: "", status: "", model: "", keyword: "" });
                setPage(1);
              }}
            >
              重置
            </Button>
          </div>
        </div>

        {isPending ? (
          <EmptyState>加载中…</EmptyState>
        ) : !data || data.items.length === 0 ? (
          <EmptyState>没有匹配的记录</EmptyState>
        ) : (
          <>
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-100">
                  <Th>ID</Th>
                  <Th>时间</Th>
                  <Th>model 字段</Th>
                  <Th>Pipeline</Th>
                  <Th>状态</Th>
                  <Th>耗时</Th>
                  <Th>tokens</Th>
                  <Th>响应预览</Th>
                  <Th></Th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={item.id} className="border-b border-slate-50">
                    <Td>#{item.id}</Td>
                    <Td className="whitespace-nowrap">{formatDateTime(item.created_at)}</Td>
                    <Td className="font-mono text-xs">{item.client_model_field}</Td>
                    <Td>{item.pipeline_name || "—"}</Td>
                    <Td>
                      <Badge tone={statusTone(item.status)}>{item.status}</Badge>
                    </Td>
                    <Td>{formatDuration(item.total_duration_ms)}</Td>
                    <Td className="text-xs">
                      {item.total_prompt_tokens}/{item.total_completion_tokens}
                    </Td>
                    <Td className="max-w-72 truncate text-xs text-slate-500">{item.content_preview || "—"}</Td>
                    <Td>
                      <Link className="text-sm text-slate-900 underline" to={`/traces/${item.id}`}>
                        详情
                      </Link>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-3 flex items-center justify-end gap-2 text-sm text-slate-500">
              <Button variant="secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                上一页
              </Button>
              <span>
                {page} / {totalPages}
              </span>
              <Button variant="secondary" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
                下一页
              </Button>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
