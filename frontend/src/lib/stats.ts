/** 用量统计看板的纯函数：命中率/未命中计算、数字格式化、CSV 导出（T41）。 */

export interface UsageMetrics {
  calls: number;
  failed_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

/**
 * 缓存命中率 = 缓存命中 tokens / 计费输入总量。prompt=0 时返回 null（无输入不谈命中率）。
 * prompt_tokens 恒含命中/写入（归一化语义，见后端 adapters.base.LlmUsage）。
 */
export function hitRate(metrics: Pick<UsageMetrics, "prompt_tokens" | "cached_tokens">): number | null {
  if (metrics.prompt_tokens <= 0) return null;
  return metrics.cached_tokens / metrics.prompt_tokens;
}

/** 未命中输入 = 计费输入总量 - 命中 - 写入（下限 0，防上游口径异常出现负数）。 */
export function missTokens(
  metrics: Pick<UsageMetrics, "prompt_tokens" | "cached_tokens" | "cache_write_tokens">,
): number {
  return Math.max(metrics.prompt_tokens - metrics.cached_tokens - metrics.cache_write_tokens, 0);
}

/** 整数千分位（对账要精确数字，不用缩写）。 */
export function formatTokens(value: number): string {
  return value.toLocaleString("en-US");
}

/** 命中率百分比文案；null（无输入）显示 —。 */
export function formatRate(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

export interface CsvColumn<T> {
  header: string;
  value: (row: T) => string | number;
}

/** 生成 CSV 文本（RFC 4180：引号转义 + CRLF；前缀 BOM 保证 Excel 识别 UTF-8 中文）。 */
export function toCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  const escape = (raw: string | number): string => {
    const text = String(raw);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [
    columns.map((c) => escape(c.header)).join(","),
    ...rows.map((row) => columns.map((c) => escape(c.value(row))).join(",")),
  ];
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

/** 触发浏览器下载 CSV（对账：直接拿去和供应商账单比对）。 */
export function downloadCsv(filename: string, csv: string): void {
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
