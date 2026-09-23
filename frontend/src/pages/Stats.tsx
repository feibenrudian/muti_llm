import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type StatsDailyRow, type StatsFilters, type UsageMetrics } from "../lib/api";
import {
  downloadCsv,
  formatRate,
  formatTokens,
  hitRate,
  missTokens,
  toCsv,
  type CsvColumn,
} from "../lib/stats";
import { Button, Card, EmptyState, Field, Input, Select, Td, Th } from "../components/ui";

/** 用量统计看板（T41）：按供应商/模型/天的 token 对账统计，支持 CSV 导出。 */

interface FiltersState {
  start: string;
  end: string;
  provider: string;
  model: string;
}

const EMPTY_FILTERS: FiltersState = { start: "", end: "", provider: "", model: "" };

function toParams(f: FiltersState): StatsFilters {
  return { start: f.start || undefined, end: f.end || undefined, provider: f.provider || undefined, model: f.model || undefined };
}

function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <p className="text-xs font-medium uppercase text-slate-400">{label}</p>
      <p className="mt-1 text-xl font-bold text-slate-800">{value}</p>
      {sub ? <p className="mt-0.5 text-xs text-slate-400">{sub}</p> : null}
    </div>
  );
}

const SEGMENTS = [
  { key: "cached", label: "缓存命中", color: "bg-emerald-500" },
  { key: "write", label: "缓存写入", color: "bg-amber-400" },
  { key: "miss", label: "未命中输入", color: "bg-sky-500" },
  { key: "completion", label: "输出", color: "bg-slate-400" },
] as const;

function DailyChart({ data }: { data: StatsDailyRow[] }) {
  const totals = data.map((d) => d.total_tokens);
  const max = Math.max(...totals, 1);
  return (
    <div>
      <div className="flex h-40 items-end justify-center gap-1">
        {data.map((d) => {
          const seg = (value: number) => (value / max) * 100;
          const miss = missTokens(d);
          return (
            <div
              key={d.date}
              className="flex h-full min-w-3 max-w-12 flex-1 flex-col justify-end"
              title={`${d.date}：输入 ${formatTokens(d.prompt_tokens)}（命中 ${formatTokens(d.cached_tokens)} / 写入 ${formatTokens(d.cache_write_tokens)} / 未命中 ${formatTokens(miss)}），输出 ${formatTokens(d.completion_tokens)}`}
            >
              {(["cached", "write", "miss", "completion"] as const).map((key) => {
                const value =
                  key === "cached" ? d.cached_tokens : key === "write" ? d.cache_write_tokens : key === "miss" ? miss : d.completion_tokens;
                if (value <= 0) return null;
                return (
                  <div
                    key={key}
                    className={`${SEGMENTS.find((s) => s.key === key)!.color} w-full`}
                    style={{ height: `${seg(value)}%` }}
                  />
                );
              })}
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex items-center gap-4 text-xs text-slate-500">
        {SEGMENTS.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span className={`inline-block h-2.5 w-2.5 rounded-sm ${s.color}`} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function metricsColumns<T extends UsageMetrics & { provider_name: string }>(
  nameHeader: string,
  nameOf: (row: T) => string,
): CsvColumn<T>[] {
  return [
    { header: nameHeader, value: nameOf },
    { header: "调用数", value: (r) => r.calls },
    { header: "失败数", value: (r) => r.failed_calls },
    { header: "输入tokens", value: (r) => r.prompt_tokens },
    { header: "缓存命中tokens", value: (r) => r.cached_tokens },
    { header: "缓存写入tokens", value: (r) => r.cache_write_tokens },
    { header: "未命中tokens", value: (r) => missTokens(r) },
    { header: "输出tokens", value: (r) => r.completion_tokens },
    { header: "合计tokens", value: (r) => r.total_tokens },
  ];
}

function MetricsTable<T extends UsageMetrics & { provider_name: string; upstream_model_id?: string }>({
  rows,
  nameHeader,
  nameOf,
  subNameOf,
  csvName,
  emptyText,
}: {
  rows: T[];
  nameHeader: string;
  nameOf: (row: T) => string;
  subNameOf?: (row: T) => string | null | undefined;
  csvName: string;
  emptyText: string;
}) {
  const columns = metricsColumns(nameHeader, nameOf);
  const exportCsv = () => downloadCsv(csvName, toCsv(columns, rows));
  return (
    <Card
      title={`共 ${rows.length} 行`}
      actions={
        <Button variant="secondary" onClick={exportCsv} disabled={rows.length === 0} data-testid="export-csv">
          导出 CSV
        </Button>
      }
    >
      {rows.length === 0 ? (
        <EmptyState>{emptyText}</EmptyState>
      ) : (
        <table className="w-full">
          <thead>
            <tr className="border-b border-slate-100">
              <Th>{nameHeader}</Th>
              <Th>调用</Th>
              <Th>失败</Th>
              <Th>输入</Th>
              <Th>缓存命中</Th>
              <Th>缓存写入</Th>
              <Th>未命中</Th>
              <Th>输出</Th>
              <Th>命中率</Th>
              <Th>合计</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.provider_name}-${nameOf(row)}`} className="border-b border-slate-50">
                <Td>
                  <span className="font-medium">{nameOf(row)}</span>
                  {subNameOf ? (
                    <span className="ml-2 text-xs text-slate-400">{subNameOf(row) || ""}</span>
                  ) : null}
                </Td>
                <Td>{row.calls}</Td>
                <Td>{row.failed_calls}</Td>
                <Td className="whitespace-nowrap">{formatTokens(row.prompt_tokens)}</Td>
                <Td className="whitespace-nowrap">{formatTokens(row.cached_tokens)}</Td>
                <Td className="whitespace-nowrap">{formatTokens(row.cache_write_tokens)}</Td>
                <Td className="whitespace-nowrap">{formatTokens(missTokens(row))}</Td>
                <Td className="whitespace-nowrap">{formatTokens(row.completion_tokens)}</Td>
                <Td>{formatRate(hitRate(row))}</Td>
                <Td className="font-semibold whitespace-nowrap">{formatTokens(row.total_tokens)}</Td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

export default function Stats() {
  const [filters, setFilters] = useState<FiltersState>(EMPTY_FILTERS);
  const params = toParams(filters);

  const { data: providers } = useQuery({ queryKey: ["providers"], queryFn: api.providers.list });
  const { data: models } = useQuery({ queryKey: ["models"], queryFn: api.models.list });
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

  const metrics = overview.data;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">用量统计</h1>

      <Card title="筛选（每日分组为 UTC 日）">
        <div className="grid grid-cols-5 items-end gap-3">
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
    </div>
  );
}
