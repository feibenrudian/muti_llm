import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  api,
  type StatsFilters,
  type StatsUserModelRow,
  type StatsUserRow,
} from "../lib/api";
import { downloadCsv, formatRate, formatTokens, hitRate, toCsv } from "../lib/stats";
import { Badge, Button, Card, EmptyState, Field, Input, Select, Td, Th } from "../components/ui";
import { DailyChart, KpiCard, MetricsTable } from "../components/usage";
import { userLabel } from "../lib/users";

/** 用量统计看板（T41 + T50 按用户维度）：token 对账统计，支持 CSV 导出。 */

interface FiltersState {
  start: string;
  end: string;
  provider: string;
  model: string;
  user: string;
}

const EMPTY_FILTERS: FiltersState = { start: "", end: "", provider: "", model: "", user: "" };

function toParams(f: FiltersState): StatsFilters {
  return {
    start: f.start || undefined,
    end: f.end || undefined,
    provider: f.provider || undefined,
    model: f.model || undefined,
    user: f.user || undefined,
  };
}

export default function Stats() {
  const [filters, setFilters] = useState<FiltersState>(EMPTY_FILTERS);
  const params = toParams(filters);

  const { data: providers } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const { data: models } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
  const { data: users } = useQuery({
    queryKey: ["users"],
    queryFn: () => api.users.list({ page: 1, page_size: 200 }),
    select: (data) => data.items,
  });
  const overview = useQuery({
    queryKey: ["stats", "overview", params],
    queryFn: () => api.stats.overview(params),
  });
  const daily = useQuery({ queryKey: ["stats", "daily", params], queryFn: () => api.stats.daily(params) });
  const byProvider = useQuery({
    queryKey: ["stats", "by-provider", params],
    queryFn: () => api.stats.byProvider(params),
  });
  const byModel = useQuery({ queryKey: ["stats", "by-model", params], queryFn: () => api.stats.byModel(params) });
  const byUser = useQuery({ queryKey: ["stats", "by-user", params], queryFn: () => api.stats.byUser(params) });
  const byUserModel = useQuery({
    queryKey: ["stats", "by-user-model", params],
    queryFn: () => api.stats.byUserModel(params),
  });

  const metrics = overview.data;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">用量统计</h1>

      <Card title="筛选（每日分组为 UTC 日）">
        <div className="grid grid-cols-6 items-end gap-3">
          <Field label="起始日期">
            <Input type="date" value={filters.start} onChange={(e) => setFilters({ ...filters, start: e.target.value })} />
          </Field>
          <Field label="结束日期（含当天）">
            <Input type="date" value={filters.end} onChange={(e) => setFilters({ ...filters, end: e.target.value })} />
          </Field>
          <Field label="供应商">
            <Select value={filters.provider} onChange={(e) => setFilters({ ...filters, provider: e.target.value })}>
              <option value="">全部</option>
              {(providers ?? []).map((p) => (
                <option key={p.id} value={p.name}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="模型">
            <Select value={filters.model} onChange={(e) => setFilters({ ...filters, model: e.target.value })}>
              <option value="">全部</option>
              {(models ?? []).map((m) => (
                <option key={m.id} value={m.upstream_model_id}>
                  {m.display_name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="用户">
            <Select value={filters.user} onChange={(e) => setFilters({ ...filters, user: e.target.value })} data-testid="stats-user-filter">
              <option value="">全部</option>
              {(users ?? []).map((u) => (
                <option key={u.id} value={String(u.id)}>
                  {u.display_name || u.username}
                </option>
              ))}
            </Select>
          </Field>
          <div>
            <Button variant="secondary" onClick={() => setFilters(EMPTY_FILTERS)} data-testid="reset-filters">
              重置
            </Button>
          </div>
        </div>
      </Card>

      <div className="grid grid-cols-4 gap-3">
        <KpiCard
          label="调用数"
          value={formatTokens(metrics?.calls ?? 0)}
          sub={metrics ? `失败 ${formatTokens(metrics.failed_calls)}` : undefined}
        />
        <KpiCard
          label="总 tokens"
          value={formatTokens(metrics?.total_tokens ?? 0)}
          sub={metrics ? `输入 ${formatTokens(metrics.prompt_tokens)} / 输出 ${formatTokens(metrics.completion_tokens)}` : undefined}
        />
        <KpiCard
          label="缓存命中 tokens"
          value={formatTokens(metrics?.cached_tokens ?? 0)}
          sub={metrics ? `写入 ${formatTokens(metrics.cache_write_tokens)}` : undefined}
        />
        <KpiCard label="缓存命中率" value={formatRate(metrics ? hitRate(metrics) : null)} sub="命中 / 计费输入总量" />
      </div>

      <Card title="每日趋势（tokens）">
        {daily.data && daily.data.length > 0 ? (
          <DailyChart data={daily.data} />
        ) : (
          <EmptyState>{daily.isPending ? "加载中…" : "所选时间范围内没有调用"}</EmptyState>
        )}
      </Card>

      <MetricsTable
        rows={byProvider.data ?? []}
        nameHeader="供应商"
        nameOf={(r) => r.provider_name}
        csvName="usage-by-provider.csv"
        emptyText={byProvider.isPending ? "加载中…" : "所选时间范围内没有调用"}
      />

      <MetricsTable
        rows={byModel.data ?? []}
        nameHeader="模型"
        nameOf={(r) => r.upstream_model_id}
        subNameOf={(r) => r.display_name}
        csvName="usage-by-model.csv"
        emptyText={byModel.isPending ? "加载中…" : "所选时间范围内没有调用"}
      />

      <ByUserTables
        byUser={byUser.data ?? []}
        byUserPending={byUser.isPending}
        byUserModel={byUserModel.data ?? []}
        byUserModelPending={byUserModel.isPending}
      />
    </div>
  );
}

function ByUserTables({
  byUser,
  byUserPending,
  byUserModel,
  byUserModelPending,
}: {
  byUser: StatsUserRow[];
  byUserPending: boolean;
  byUserModel: StatsUserModelRow[];
  byUserModelPending: boolean;
}) {
  const emptyText = (pending: boolean) => (pending ? "加载中…" : "所选时间范围内没有调用");
  return (
    <>
      <Card
        title={`按用户（共 ${byUser.length} 行）`}
        actions={
          <Button
            variant="secondary"
            disabled={byUser.length === 0}
            onClick={() =>
              downloadCsv(
                "usage-by-user.csv",
                toCsv(
                  [
                    { header: "用户", value: (r: StatsUserRow) => userLabel(r) },
                    { header: "调用数", value: (r: StatsUserRow) => r.calls },
                    { header: "失败数", value: (r: StatsUserRow) => r.failed_calls },
                    { header: "输入tokens", value: (r: StatsUserRow) => r.prompt_tokens },
                    { header: "输出tokens", value: (r: StatsUserRow) => r.completion_tokens },
                    { header: "合计tokens", value: (r: StatsUserRow) => r.total_tokens },
                  ],
                  byUser,
                ),
              )
            }
            data-testid="export-user-csv"
          >
            导出 CSV
          </Button>
        }
      >
        {byUser.length === 0 ? (
          <EmptyState>{emptyText(byUserPending)}</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>用户</Th>
                <Th>调用</Th>
                <Th>失败</Th>
                <Th>输入</Th>
                <Th>输出</Th>
                <Th>合计</Th>
              </tr>
            </thead>
            <tbody>
              {byUser.map((row) => (
                <tr key={row.user_id} className="border-b border-slate-50" data-testid="stats-user-row">
                  <Td>
                    <span className="font-medium">{userLabel(row)}</span>
                    {row.user_id === 0 ? null : <Badge>{`#${row.user_id}`}</Badge>}
                  </Td>
                  <Td>{row.calls}</Td>
                  <Td>{row.failed_calls}</Td>
                  <Td className="whitespace-nowrap">{formatTokens(row.prompt_tokens)}</Td>
                  <Td className="whitespace-nowrap">{formatTokens(row.completion_tokens)}</Td>
                  <Td className="font-semibold whitespace-nowrap">{formatTokens(row.total_tokens)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card
        title={`按用户 × 模型（共 ${byUserModel.length} 行）`}
        actions={
          <Button
            variant="secondary"
            disabled={byUserModel.length === 0}
            onClick={() =>
              downloadCsv(
                "usage-by-user-model.csv",
                toCsv(
                  [
                    { header: "用户", value: (r: StatsUserModelRow) => userLabel(r) },
                    { header: "供应商", value: (r: StatsUserModelRow) => r.provider_name },
                    { header: "模型", value: (r: StatsUserModelRow) => r.upstream_model_id },
                    { header: "调用数", value: (r: StatsUserModelRow) => r.calls },
                    { header: "失败数", value: (r: StatsUserModelRow) => r.failed_calls },
                    { header: "输入tokens", value: (r: StatsUserModelRow) => r.prompt_tokens },
                    { header: "输出tokens", value: (r: StatsUserModelRow) => r.completion_tokens },
                    { header: "合计tokens", value: (r: StatsUserModelRow) => r.total_tokens },
                  ],
                  byUserModel,
                ),
              )
            }
            data-testid="export-user-model-csv"
          >
            导出 CSV
          </Button>
        }
      >
        {byUserModel.length === 0 ? (
          <EmptyState>{emptyText(byUserModelPending)}</EmptyState>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                <Th>用户</Th>
                <Th>供应商</Th>
                <Th>模型</Th>
                <Th>调用</Th>
                <Th>失败</Th>
                <Th>输入</Th>
                <Th>输出</Th>
                <Th>合计</Th>
              </tr>
            </thead>
            <tbody>
              {byUserModel.map((row) => (
                <tr
                  key={`${row.user_id}-${row.provider_name}-${row.upstream_model_id}`}
                  className="border-b border-slate-50"
                >
                  <Td>{userLabel(row)}</Td>
                  <Td>{row.provider_name}</Td>
                  <Td>
                    {row.upstream_model_id}
                    {row.display_name ? (
                      <span className="ml-2 text-xs text-slate-400">{row.display_name}</span>
                    ) : null}
                  </Td>
                  <Td>{row.calls}</Td>
                  <Td>{row.failed_calls}</Td>
                  <Td className="whitespace-nowrap">{formatTokens(row.prompt_tokens)}</Td>
                  <Td className="whitespace-nowrap">{formatTokens(row.completion_tokens)}</Td>
                  <Td className="font-semibold whitespace-nowrap">{formatTokens(row.total_tokens)}</Td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  );
}
