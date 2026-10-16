/** 用量看板共享展示组件（T41/T52）：KPI 卡、按天堆叠柱状图、指标表格（管理台与门户共用）。 */

import { Button, Card, EmptyState, Td, Th } from "./ui";
import {
  downloadCsv,
  formatRate,
  formatTokens,
  hitRate,
  missTokens,
  toCsv,
  type CsvColumn,
  type UsageMetrics,
} from "../lib/stats";
import type { StatsDailyRow } from "../lib/api";

export function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
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

export function DailyChart({ data }: { data: StatsDailyRow[] }) {
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
                  key === "cached"
                    ? d.cached_tokens
                    : key === "write"
                      ? d.cache_write_tokens
                      : key === "miss"
                        ? miss
                        : d.completion_tokens;
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

export function MetricsTable<
  T extends UsageMetrics & { provider_name: string; upstream_model_id?: string },
>({
  rows,
  nameHeader,
  nameOf,
  subNameOf,
  csvName,
  emptyText,
  testId,
}: {
  rows: T[];
  nameHeader: string;
  nameOf: (row: T) => string;
  subNameOf?: (row: T) => string | null | undefined;
  csvName: string;
  emptyText: string;
  /** 表格容器的 data-testid（导出按钮固定为 export-csv）。 */
  testId?: string;
}) {
  const columns = metricsColumns(nameHeader, nameOf);
  const exportCsv = () => downloadCsv(csvName, toCsv(columns, rows));
  return (
    <div data-testid={testId}>
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
    </div>
  );
}
