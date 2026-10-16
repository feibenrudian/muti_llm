/** 门户用量统计（T52）：当前用户自己的 token 用量——总览 KPI + 每日趋势 + 按模型明细。
 *
 * 聚合口径与服务器端管理台一致；归属由后端强制为登录用户，页面不提供用户/供应商管理维度。
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type StatsFilters } from "../lib/api";
import { formatRate, formatTokens, hitRate } from "../lib/stats";
import { Button, Card, EmptyState, Field, Input } from "../components/ui";
import { DailyChart, KpiCard, MetricsTable } from "../components/usage";

interface FiltersState {
  start: string;
  end: string;
}

const EMPTY_FILTERS: FiltersState = { start: "", end: "" };

function toParams(f: FiltersState): StatsFilters {
  return { start: f.start || undefined, end: f.end || undefined };
}

export default function PortalUsage() {
  const [filters, setFilters] = useState<FiltersState>(EMPTY_FILTERS);
  const params = toParams(filters);

  const overview = useQuery({
    queryKey: ["portal-stats", "overview", params],
    queryFn: () => api.portal.stats.overview(params),
  });
  const daily = useQuery({
    queryKey: ["portal-stats", "daily", params],
    queryFn: () => api.portal.stats.daily(params),
  });
  const byModel = useQuery({
    queryKey: ["portal-stats", "by-model", params],
    queryFn: () => api.portal.stats.byModel(params),
  });

  const metrics = overview.data;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">我的用量统计</h1>

      <Card title="筛选（每日分组为 UTC 日，结束日期含当天）">
        <div className="grid grid-cols-3 items-end gap-3">
          <Field label="起始日期">
            <Input
              type="date"
              value={filters.start}
              onChange={(e) => setFilters({ ...filters, start: e.target.value })}
              data-testid="portal-stats-start"
            />
          </Field>
          <Field label="结束日期（含当天）">
            <Input
              type="date"
              value={filters.end}
              onChange={(e) => setFilters({ ...filters, end: e.target.value })}
              data-testid="portal-stats-end"
            />
          </Field>
          <div>
            <Button
              variant="secondary"
              onClick={() => setFilters(EMPTY_FILTERS)}
              data-testid="portal-stats-reset"
            >
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
          sub={
            metrics
              ? `输入 ${formatTokens(metrics.prompt_tokens)} / 输出 ${formatTokens(metrics.completion_tokens)}`
              : undefined
          }
        />
        <KpiCard
          label="缓存命中 tokens"
          value={formatTokens(metrics?.cached_tokens ?? 0)}
          sub={metrics ? `写入 ${formatTokens(metrics.cache_write_tokens)}` : undefined}
        />
        <KpiCard
          label="缓存命中率"
          value={formatRate(metrics ? hitRate(metrics) : null)}
          sub="命中 / 计费输入总量"
        />
      </div>

      <Card title="每日趋势（tokens）">
        {daily.data && daily.data.length > 0 ? (
          <DailyChart data={daily.data} />
        ) : (
          <EmptyState>{daily.isPending ? "加载中…" : "所选时间范围内没有调用"}</EmptyState>
        )}
      </Card>

      <MetricsTable
        rows={byModel.data ?? []}
        nameHeader="模型"
        nameOf={(r) => r.upstream_model_id}
        subNameOf={(r) => r.display_name}
        csvName="my-usage-by-model.csv"
        emptyText={byModel.isPending ? "加载中…" : "所选时间范围内没有调用"}
        testId="portal-usage-by-model"
      />
    </div>
  );
}
