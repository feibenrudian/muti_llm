import { describe, expect, test } from "vitest";
import { formatRate, formatTokens, hitRate, missTokens, toCsv } from "./stats";

describe("hitRate（UT-41-4 命中率与未命中计算）", () => {
  test("命中/写入均从计费输入总量中拆出", () => {
    expect(hitRate({ prompt_tokens: 1000, cached_tokens: 250 })).toBe(0.25);
    expect(
      missTokens({ prompt_tokens: 1000, cached_tokens: 250, cache_write_tokens: 100 }),
    ).toBe(650);
  });

  test("输入为 0 时命中率为 null，未命中为 0", () => {
    expect(hitRate({ prompt_tokens: 0, cached_tokens: 0 })).toBeNull();
    expect(
      missTokens({ prompt_tokens: 0, cached_tokens: 0, cache_write_tokens: 0 }),
    ).toBe(0);
  });

  test("上游口径异常（命中 > 输入）时未命中不下探为负", () => {
    expect(missTokens({ prompt_tokens: 100, cached_tokens: 200, cache_write_tokens: 0 })).toBe(0);
  });
});

describe("formatTokens / formatRate（UT-41-4 展示格式化）", () => {
  test("千分位整数、命中率百分比、无输入显示 —", () => {
    expect(formatTokens(1234567)).toBe("1,234,567");
    expect(formatRate(0.256)).toBe("25.6%");
    expect(formatRate(null)).toBe("—");
  });
});

describe("toCsv（UT-41-4 CSV 导出）", () => {
  test("BOM + 表头 + 行转义", () => {
    interface Row {
      name: string;
      tokens: number;
      note: string;
    }
    const rows: Row[] = [
      { name: "srs供应商", tokens: 100, note: '含"逗号",换行\n符' },
      { name: "b", tokens: 2, note: "" },
    ];
    const csv = toCsv(
      [
        { header: "供应商", value: (r) => r.name },
        { header: "tokens", value: (r) => r.tokens },
        { header: "备注", value: (r) => r.note },
      ],
      rows,
    );
    expect(csv.charCodeAt(0)).toBe(0xfeff); // BOM
    const body = csv.slice(1);
    const lines = body.split("\r\n");
    expect(lines[0]).toBe("供应商,tokens,备注");
    expect(lines[1]).toBe('srs供应商,100,"含""逗号"",换行\n符"');
    expect(lines[2]).toBe("b,2,");
  });
});
